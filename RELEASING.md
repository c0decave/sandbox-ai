# Releasing sandbox-ai

The version lives in one place: `__version__` in `sandbox/agent_sandbox.py`.
Raise it with `scripts/bump_version.py` for every change you install or ship:
the patch counts up to 250, then the minor goes up (`0.1.250` → `0.2.0`).
`--minor`, `--major`, `--set X.Y.Z` and `--show` cover the rest.

A release consists of these files - the list is `sandbox/release_assets.py`, and `scripts/make_dist.py`,
`scripts/publish_release.py`, `scripts/release_check.py`, `scripts/release_notes.py` and the updaters take the names
from it (`tests/test_release_assets.py`):

| File | Built by | Used by | Who updates with it |
|---|---|---|---|
| `sandbox-ai-bundle-X.Y.Z.tar.gz` | `make_dist.py` (CI: job `build`) | `install.sh` (the whole tree, reproducible) | install.sh's installations: `sandbox-ai update`, a system one `sudo sandbox-ai update`, other hosts `sandbox-ai hosts update NAME` |
| `sandbox_ai_app-X.Y.Z-py3-none-any.whl` | `make_dist.py` | PyPI project `sandbox-ai-app` (`pip install sandbox-ai-app`: the command line and the web interface; `pip install 'sandbox-ai-app[gui]'`: with the desktop app) | pip and pipx installations: `sandbox-ai update` installs this file with the environment's own pip |
| `sandbox_ai_app-X.Y.Z.tar.gz` | `make_dist.py` | PyPI (the source package) | - |
| `install.sh` | `make_dist.py` | first installs (`curl … | bash`); carries the release keys of `sandbox/release-signers` | - |
| `sandbox-ai.pyz` | `make_dist.py` (`make_pyz.py`) | the command line in one file (`./sandbox-ai.pyz …` or `python3 -I sandbox-ai.pyz …`) | the `.pyz`: `./sandbox-ai.pyz update` replaces the file |
| `sandbox-ai-x86_64.AppImage`, `sandbox-ai-aarch64.AppImage` | `release.yml` (jobs `build-appimage`) | the app with sandbox-ai, Python and PySide6 inside, for Linux | the AppImage: `./sandbox-ai-ARCH.AppImage cli update` (the app: Settings › Updates) replaces the file |
| `sandbox-ai-arm64.dmg`, `sandbox-ai-x86_64.dmg` | `release.yml` (jobs `build-macos`) | `sandbox-ai.app` for macOS (not notarised) | the macOS app: `sandbox-ai.app/Contents/MacOS/sandbox-ai update` (the app: Settings › Updates) replaces `sandbox-ai.app` with the one in the disk image |
| `sandbox-ai-videos.tar` | the maintainer (`make_dist.py --videos DIR`, see below) | the videos about the app's pages, which the app downloads once | the app (once per version) |
| `SHA256SUMS` | `make_dist.py` (`--sums-only` over everything) | every updater checks its file against it; it lists every file above | every kind |
| `SHA256SUMS.sig` | the maintainer (`--sign KEY`) | every updater checks `SHA256SUMS` with it: an SSH signature by a release key | every kind |

Every kind of installation updates itself from these files (`sandbox-ai update`; in the app Settings › Updates ›
Install now…): the release is found through the releases API of the source (`releases/latest` - GitHub by default,
`https://github.com/c0decave/sandbox-ai`; `--source URL`, `update_source` or `SANDBOX_AI_SOURCE` names a Gitea or
Forgejo), `SHA256SUMS.sig` must be a good signature of `SHA256SUMS` by a key of the installed copy's
`sandbox/release-signers`, and the file must have the SHA-256 listed there. The version of a release is the one in its
bundle's name, which the signed `SHA256SUMS` lists: **a release without its bundle, without `SHA256SUMS` or without
the file of a kind cannot update that kind**, and a release that changes a name breaks every installation that asks
for the old one. `tests/test_update_github.py` (and the app's `tests/test_update_github_e2e.py`) run every kind's
update against a fake GitHub, with a release built by `make_dist.py`; one of them updates the files of the last
public release (0.1.94: `tests/golden/release-0.1.94`) to the next one.

The file names carry no version where they need not, so `https://github.com/c0decave/sandbox-ai/releases/latest/download/NAME`
always names the newest (the README's install commands use that).

## Steps

```bash
# 0. On a host with Podman and Bubblewrap (not in a sandbox): the live check, its report under
#    ~/.local/state/sandbox-ai/live-check/ (scripts/live_check.py --help)
scripts/live_check.py --build
#    and what the development sandbox could not run (the security reviews' release gates, Firejail's blacklist, the
#    firewall for a login's server, logins given while running, --hide on workspace add, Landlock with a real HOME,
#    kits and plugins attached live, devices): live-check/REPORT-DATE.md (docs/LIVE_CHECK.md)
scripts/live_check.sh
#    and the agents and parts not live-tested before (Hermes, Qwen Code, DSH, Goose, nanobot, Letta Code, Pi, Cairn, the sandbox's own
#    browser): their images built, each against a scripted model on this host, one MCP tool, its switches' hosts,
#    no telemetry; send back the report.md it names (CI's job agents-live runs the same)
scripts/live_check_agents.sh

# 1. Bump the version, commit, tag
v=$(python3 scripts/bump_version.py)
python3 -m pytest -q tests
git commit -am "Release $v"
git tag "v$v"
git push origin main "v$v"

# 2. Build dist/ and sign SHA256SUMS (needs build and setuptools, as CI pins them:
#    python3 -m pip install --require-hashes --only-binary :all: -r packaging/requirements-ci.txt)
python3 scripts/make_dist.py --no-isolation --sign ~/.config/sandbox-ai/release-key

# 3. Publish these files as a release on your own Gitea/Forgejo (SANDBOX_AI_SOURCE names it; a token that may
#    write releases). --partial: step 2 builds no AppImage, no disk image and no videos, and publish_release.py
#    refuses an incomplete release without it. A release on GitHub: "The checklist of a release on GitHub" below.
export SANDBOX_AI_TOKEN=...
python3 scripts/publish_release.py --source "$SANDBOX_AI_SOURCE" --partial dist/*
```

`sandbox/release-signers` lists the release key since 0.1.92: sign every release (installations that trust it
refuse an unsigned one).

A release is complete or it is not published: `publish_release.py` refuses files that are not every file of the
table above (each lacking one is named, nothing is uploaded) and a file of another version. `--partial` uploads
without them to a Gitea or Forgejo of your own - step 2 alone builds no AppImage and no disk image, so a release
there says what it goes up without - and is refused for GitHub. A new release is made as a **draft** and published
after its last file is up, so `releases/latest` (what every installation asks) never names a release whose files are
still on their way; `--draft` leaves it a draft for the check, `--publish` ends it.

## The checklist of a release on GitHub

A pushed tag is not a release: `release.yml` builds and checks, the release itself is signed and published from your
machine, in the same sitting. Every step stops when something is missing.

```bash
v=X.Y.Z; src=https://github.com/c0decave/sandbox-ai; run=RUN_ID   # the run of release.yml for the tag v$v
export GH_HOST=github.com                                          # gh: GitHub, never another host
gh run view "$run" --json jobs --jq '.jobs[] | "\(.conclusion)\t\(.name)"'   # 1. every job: success
gh run download "$run" -n dist -D dist                             # 2. what CI built and checked
gh run download "$run" -p 'appimage-*' -p 'macos-*' -D packages
python3 scripts/make_dist.py --sums-only --out dist --add packages/*/* \
    --videos ~/videos-$v --sign ~/.config/sandbox-ai/release-key  # 3. the videos, SHA256SUMS, its signature
python3 scripts/release_check.py "$v" --dir dist                   # 4. every file there, listed, signed, works
export SANDBOX_AI_TOKEN=...                                        #    (a token that may write releases)
python3 scripts/release_notes.py --version "$v" > notes.md          #    the page (at most 125,000 characters)
python3 scripts/publish_release.py --source "$src" --draft \
    --notes-file notes.md dist/*                                   # 5. a draft: nobody is served it
python3 scripts/release_check.py "$v" --source "$src"              # 6. the draft, downloaded as an update would
python3 scripts/publish_release.py --source "$src" --publish       # 7. published: releases/latest names it
env -u SANDBOX_AI_TOKEN python3 scripts/release_check.py "$v" --source "$src"   # 8. as everybody sees it
sandbox-ai update --check                                          # 9. an installation sees it, signed
```

- [ ] Before the tag: `python3 -m pytest -q tests` (it runs every kind's update against a fake GitHub),
      `sandbox-ai privacy-check`, `docs/releases/vX.Y.Z.md` written, the videos rendered (below).
- [ ] 1: `build`, `e2e-pyz` (3.9, 3.13), `build-appimage` and `e2e-appimage` (x86_64, aarch64; ubuntu:22.04,
      debian:12), `build-macos` and `e2e-macos` (arm64, x86_64), `release` (its step "Every file CI builds is there,
      listed in SHA256SUMS and works"), `pypi` - all green. A red job: fix, tag again; never publish around it.
- [ ] 4 and 6 end with `sandbox-ai X.Y.Z: READY` and list twelve files: `install.sh`, `sandbox-ai-x86_64.AppImage`,
      `sandbox-ai-aarch64.AppImage`, `sandbox-ai-arm64.dmg`, `sandbox-ai-x86_64.dmg`, `sandbox-ai.pyz`,
      `sandbox_ai_app-X.Y.Z-py3-none-any.whl`, `sandbox_ai_app-X.Y.Z.tar.gz`, `sandbox-ai-bundle-X.Y.Z.tar.gz`,
      `sandbox-ai-videos.tar`, `SHA256SUMS`, `SHA256SUMS.sig` - each there, listed, its SHA-256 right, its try ok.
- [ ] 8 says `published, and the newest release (what installations are served)`.
- [ ] 9, on an installation of the release before: `available: X.Y.Z` and `signature: good, by the trusted key …`;
      then `sandbox-ai update` there, and on a Mac the app's Settings › Updates › Install now… (the macOS update is
      the one path no test here runs on a real macOS: the tests' `hdiutil` is a stand-in).
- [ ] The MCP catalog and anything else the release needs signed: in the same sitting.

`scripts/release_check.py` says per file: there, listed in `SHA256SUMS`, its SHA-256, and a quick try - the bundle
(its version; it trusts the key that signed the release, or the installations it makes would refuse the next one),
`install.sh` (`bash -n`; it carries the signing key for first installs), the `.pyz` (started: `--version`), the
AppImage of this machine's architecture (`--appimage-extract` of its manifest, which names the version), the disk
images (their form; opened only on macOS), wheel and sdist (the metadata), the videos (only the series' files).
`--dir DIR` checks a folder, `--source URL` a release (a draft with `SANDBOX_AI_TOKEN`), `--ci` what `release.yml`
built (no videos, no signature), `--json` prints the report for programs, `--no-try` starts nothing.

## The release page

`scripts/release_notes.py` writes the text of the release page (release.yml passes it to `gh release create`, and
to Gitea/Forgejo through `publish_release.py --notes`): what is new, a table of every file with who it is for,
how to install and start (install.sh, AppImage, the macOS app, PyPI, the `.pyz`), the first steps, how to update
and how to check the files. What is new comes from `docs/releases/vX.Y.Z.md`; write it before you tag (a list of
the changes users notice). `python3 scripts/release_notes.py` shows the page.

## Documentation, the learning video and the screenshots

Before the tag, `python3 -m pytest -q tests` also checks the documentation that can drift: the commands the
learning video's storyboard shows exist (`tests/test_video_storyboard.py`), the release notes name real commands
(`tests/test_release_notes.py`), and lines the READMEs must keep. Update both READMEs (English and German, section by
section) and the quickstarts with the release's changes, and refresh `docs/screenshots/` when a page they show
changed (`docs/video/capture_app.py`; see [docs/video/README.md](docs/video/README.md)). The video itself is not
kept in Git: after the tag, render it with the release's checkout (`docs/video/make_video.py --out DIR`) and
attach the MP4 and VTT files to the release.

### The videos about the app's pages (optional release file)

The desktop app plays the series *The desktop app, page by page* (one video per page) in its own player, and
downloads it once from the release of its version when it is not on the computer. That download is the release file
`sandbox-ai-videos.tar`: the series' `sandbox-ai-app-NN-PAGE-LANG.mp4` and `.vtt` files, uncompressed (MP4 is
compressed already), sorted, with a fixed owner, mode and time. CI cannot render it, so it is added where the release
is signed; a release on GitHub always carries it (a release without it works, the app then says the release has no
videos - `publish_release.py --partial`, for your own Gitea). Like every file it is listed in `SHA256SUMS`: the app installs it only after the
signature of `SHA256SUMS` is good by a key of the installed `sandbox/release-signers`, the archive has the SHA-256
listed there and it holds nothing but the series' files.

```bash
# render the series with the release's checkout (docs/video/README.md: the tools and the voice)
/tmp/video-venv/bin/python docs/video/make_video.py --out ~/videos-X.Y.Z --series pages --lang de \
    --voice /tmp/piper-voices/de_DE-thorsten-high.onnx
# put the archive into dist/ with the rest and sign SHA256SUMS over all of it
python3 scripts/make_dist.py --no-isolation --videos ~/videos-X.Y.Z --sign ~/.config/sandbox-ai/release-key
#   or, to a dist/ that CI built (see "Sign" below):
python3 scripts/make_dist.py --sums-only --out dist --add packages/*/* --videos ~/videos-X.Y.Z --sign ~/.config/sandbox-ai/release-key
python3 scripts/publish_release.py --source https://github.com/c0decave/sandbox-ai dist/*
```

`make_dist.py --videos DIR` takes only the files with the series' names from DIR (nothing else ends up in the
archive) and stops when DIR holds no video. `publish_release.py` refuses to upload `sandbox-ai-videos.tar` that
`SHA256SUMS` does not list, and a release for GitHub without it. To check the archive by hand: `sha256sum --ignore-missing -c SHA256SUMS`, then
`tar tf sandbox-ai-videos.tar`. Users who have the files already put them into `~/.local/share/sandbox-ai/videos`
(or name a folder with `SANDBOX_AI_VIDEOS`); the app does not download then.

## The ready-made packages

| File | Built by | Where |
|---|---|---|
| `sandbox-ai.pyz` | `scripts/make_pyz.py` (called by `make_dist.py`; `--skip-pyz` leaves it out) | anywhere |
| `sandbox-ai-ARCH.AppImage` | `scripts/build_appimage.sh --bundle dist/sandbox-ai-bundle-X.Y.Z.tar.gz` | Linux of that architecture |
| `sandbox-ai-ARCH.dmg` | `scripts/build_macos_app.sh --bundle dist/sandbox-ai-bundle-X.Y.Z.tar.gz` | macOS of that architecture |

All three are made from the bundle, so they carry exactly the tree `install.sh` would install, with the app
(`projects/sandbox-ai-app`, part of this repository since 0.1.66: the bundle takes it from the same checkout). Each package has a
`PACKAGING` file beside `sandbox/`, so `sandbox-ai update` replaces the package with the release's new one
(`sandbox/package_update.py`) instead of running `install.sh`: the file it was started from (the `.pyz`, the
AppImage: started once with `--version` before it takes the old one's place) or `sandbox-ai.app` (from the disk
image, with `hdiutil` and `ditto`); the one before is kept in `~/.cache/sandbox-ai/update-previous` for
`sandbox-ai update --rollback`. A package in a folder its user cannot write says the command that can
(`sudo -H … update`). `scripts/e2e_package.sh KIND FILE` checks a package the way a user starts it (version, presets,
doctor, a dry run, the copy in the cache, the app's `--self-test`); `release.yml` runs it for every package, the
AppImage also in `ubuntu:22.04` and `debian:12` containers.

- **`.pyz`:** a zip archive behind a `#!/bin/sh` line that runs `python3 -I` on it, reproducible on any machine
  (sorted, stored entries: deflate's output differs between zlib and zlib-ng, so nothing is compressed, about
  7 MB; fixed times from `SOURCE_DATE_EPOCH` or the bundle). The bundle `.tar.gz` itself is still gzip: two
  machines with different zlib builds can make different bundles from one commit. `scripts/dist_boot.py` is its `__main__.py`: it extracts the tree with its
  `manifest.json` (SHA-256 of every file) into `~/.cache/sandbox-ai/pyz/`, compiles it, makes it read-only and
  records each file's inode and change time; every start checks that record and refuses a changed copy (one
  that lost its record, e.g. to a cache cleaner, is unpacked again).
- **AppImage:** python-build-standalone, PySide6-Essentials and shiboken6 (`pip --require-hashes`), the bundle,
  compiled once; `AppRun` runs the same `dist_boot.py`, which copies the Python and the tree into
  `~/.cache/sandbox-ai/appimage/` (sessions outlive the AppImage's mount) and starts the app, or the command line
  for `cli ARGS…` and when called as `sandbox-ai`. appimagetool and its runtime are pinned releases.
- **macOS:** the same Python and PySide6 (universal2 wheels) in `sandbox-ai.app/Contents/Resources`, the
  launchers `Contents/MacOS/sandbox-ai-app` (the app) and `Contents/MacOS/sandbox-ai` (the command line), the
  icon rendered from `sandbox/web/icon.svg` (`scripts/render_icon.py`, `iconutil`), an ad-hoc signature, in a
  disk image made by `hdiutil`. There is no Apple developer ID: the app is not notarised, and the README explains
  the first start (right-click > Open, Open Anyway, or `xattr -dr com.apple.quarantine`).

**Pins.** CI installs pytest, build and setuptools only from `packaging/requirements-ci.txt` (hashes;
`make_dist.py --no-isolation` builds with that setuptools). Every third-party download is pinned by release, exact file name and SHA-256: python-build-standalone,
appimagetool and the AppImage runtime in `packaging/pins.sh`, PySide6 in `packaging/requirements-app.txt`, the
GitHub actions by commit SHA in the workflows. A release is pinned only once it is at least 14 days old, with its
publish date beside the pin; `tests/test_dist_packaging.py` checks all of it. To move a pin: look up the release's
date (GitHub `published_at`, PyPI `upload_time`), its files' SHA-256 (the release's own `SHA256SUMS` or GitHub's
asset digest, PyPI's `digests`), update the pin, the date and `PINNED_ON`, and build once.

The AppImage is reproducible: two builds of one bundle give the same file (checked locally, x86_64): no path
of the build's folder inside (the build stops otherwise, and `e2e_package.sh` greps the extracted AppImage), every
`.pyc` hash-based, every file time `SOURCE_DATE_EPOCH` (default: the bundle's).

PySide6-WebEngine (the app's terminal tabs) is left out of the packages on purpose: with PySide6-Addons it would
almost triple their size for one optional page (see `packaging/requirements-app.txt`).

`install.sh` and `sandbox-ai-update` install from `https://github.com/c0decave/sandbox-ai` unless
`--source URL` or `SANDBOX_AI_SOURCE` names another repository; keep
`SANDBOX_AI_SOURCE` set in your shell when your own installation updates from
your own server.

`make_dist.py` refuses uncommitted changes (`--allow-dirty` for a local test)
and builds the bundle from the files git tracks, with fixed owner and
timestamps (`SOURCE_DATE_EPOCH`, default: the last commit), so two builds of
one commit are identical. `publish_release.py` creates the release for the
tag if it does not exist and replaces files of the same name.

Users then update with `sandbox-ai-update`; new installs use
`./install.sh` (or `install.sh --source URL`).

## Signed releases

`SHA256SUMS` comes from the same release as the bundle, so it only catches
damaged downloads. The signature catches the rest: whoever takes over the
GitHub account, the release or the download path cannot sign without the
release key, and installations refuse what it did not sign.

How it works:

- A release signs `SHA256SUMS` with an SSH key: `ssh-keygen -Y sign -n
  sandbox-ai-release`, written to `SHA256SUMS.sig`. Only OpenSSH 8.1 or newer
  is needed, on both sides.
- `sandbox/release-signers` lists the keys that may sign, in ssh-keygen's
  allowed signers format, always as `release@sandbox-ai
  namespaces="sandbox-ai-release" ssh-ed25519 AAAA… [comment]`. The namespace
  keeps a signature made for anything else (a Git commit, a file) from counting.
- `install.sh` and `sandbox-ai update` check the signature against the keys of
  the tree that is installed **now**, then the bundle against `SHA256SUMS`.
  Once the installed tree lists a key, an unsigned release, a release signed by
  another key and a changed `SHA256SUMS` are refused before anything is
  unpacked. The new tree's keys count from the next update on.
- The very first install has no tree yet: `make_dist.py` writes the keys of
  `sandbox/release-signers` into the published `install.sh` (and `./install.sh`
  in a checkout or unpacked bundle uses that tree's file). That is trust on
  first use: whoever served that `install.sh` chose the keys. Check it by hand
  if that matters (below).
- Releases older than the installed version are refused (`--allow-downgrade`).
  `release-state.json` in the installation remembers the newest version seen
  signed or installed; a "newest" release below it is pointed out.

### Create and register the key

```bash
scripts/release_key.sh new                  # ~/.config/sandbox-ai/release-key (0600), asks for a passphrase
scripts/release_key.sh show                 # the line for sandbox/release-signers, and the fingerprint
scripts/release_key.sh show >> sandbox/release-signers
git commit -m "Release key" sandbox/release-signers
```

The private key never goes into the repository (`release_key.sh` refuses a
path inside it) and never into CI. Keep an offline backup. `--sign` also takes
the `.pub` of a key that lives in `ssh-agent` or on a FIDO token
(`ssh-keygen -t ed25519-sk`; `release_key.sh show FILE.pub` prints its line).
Publish the fingerprint somewhere else as well (your website, a signed Git
tag), so that users can compare it.

### Sign

`make_dist.py --sign KEY` signs `SHA256SUMS` and checks the signature against
`sandbox/release-signers`; it stops if the key is not listed there, because a
first install with this release's `install.sh` could not check it.
`publish_release.py` refuses to upload an unsigned `SHA256SUMS` while
`sandbox/release-signers` lists keys, checks `SHA256SUMS.sig` and every file
against `SHA256SUMS` before the release changes, and removes an old
`SHA256SUMS.sig` when it replaces `SHA256SUMS` without one. `publish_release.py
--sign KEY dist/*` signs at upload time instead.

The workflow `release.yml` holds no key: once `sandbox/release-signers` lists
one, it still tests, builds `dist/` and the packages, checks them and
publishes to PyPI, but creates no GitHub or Gitea release. Sign and publish
what it built and checked from your machine (the AppImages and disk images
contain its bundle, so sign that bundle, not one rebuilt here): "The checklist
of a release on GitHub" above, in short

```bash
GH_HOST=github.com gh run download RUN_ID -n dist -D dist           # the run of the tag's release.yml
GH_HOST=github.com gh run download RUN_ID -p 'appimage-*' -p 'macos-*' -D packages
python3 scripts/make_dist.py --sums-only --out dist --add packages/*/* --videos ~/videos-X.Y.Z --sign ~/.config/sandbox-ai/release-key
python3 scripts/release_check.py --dir dist
python3 scripts/publish_release.py --source https://github.com/c0decave/sandbox-ai dist/*
```

`--sums-only` keeps `dist/` as it is, `--add` copies the packages in, and
`SHA256SUMS` (with its signature) then covers every file. `make_dist.py`
builds the same bundle and `.pyz` from the same commit (the app is part of it;
reproducible), so you can compare them with CI's before you sign. Before that, a key stored as a CI secret would make the signature
worth as much as the account it should protect against.

### Rotate

Installed versions trust only the keys they have, so a new key needs two
releases:

1. Add the new key to `sandbox/release-signers`, sign the release with the
   **old** key. Installations check it with the old key and trust both from
   then on.
2. Later, remove the old key and sign with the new one.

Leave time between the two: an installation that skipped release 1 refuses
release 2 (`signed by SHA256:…, which is not a trusted release key`); it gets
there with `sandbox-ai update --version <release 1>` first, then `sandbox-ai
update`.

### Revoke

If a key is lost or stolen, sign the next release with another listed key and
remove the bad one from `sandbox/release-signers`; installations drop it with
that update. Hence: keep two keys listed (one of them offline), so that one can
revoke the other. An installation that has not updated yet still trusts the
revoked key; that is the limit of keys that ship with the tree.

If the only key is compromised, no signature can tell the attacker's releases
from yours. Make a new key, publish a release with only that key, signed by
it, publish the new fingerprint elsewhere, and ask users to check the release
by hand (below) and install it with `install.sh --from
sandbox-ai-bundle-X.Y.Z.tar.gz`: installations refuse it otherwise, and
`--allow-unsigned` never lets a release change the keys.

### Check a release by hand

```bash
printf 'release@sandbox-ai namespaces="sandbox-ai-release" %s\n' "ssh-ed25519 AAAA…" > signers
ssh-keygen -Y verify -f signers -I release@sandbox-ai -n sandbox-ai-release -s SHA256SUMS.sig < SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS
```

This checks the published `install.sh` before a first `curl … | bash` too.

### Limits

- Trust on first use: the first `install.sh`, a checkout or a version from
  before signatures decides the keys. Updating from 0.1.27 or older runs the
  old installer, which does not check signatures yet; the version it installs
  does from then on.
- A source can withhold new releases (freeze): an installation sees no newer
  version and cannot tell. It can serve an old signed release as the newest:
  refused below the installed version, pointed out below the newest seen.
- `--from DIR` installs what you point at; `--from BUNDLE.tar.gz` is checked
  only with `--verify` (or under the policy below). Neither is a downgrade check.
- The keys and `release-state.json` live in the installation, writable by its
  user: they protect against the network and the release host, not against
  that user.
- An administrator can make signatures mandatory for everybody on the machine
  with `"require_signed_updates": true` in `/etc/sandbox-ai/policy.json`:
  `--allow-unsigned` is refused, an installation without a trusted key cannot
  update, and `--from DIR` is refused (`--from BUNDLE` is verified).
- A pip installation updated with `sandbox-ai update` takes the release's wheel, checked with these signatures
  (its `[gui]` PySide6 still comes from PyPI when the release moves its pins); one updated with `pip install -U
  sandbox-ai-app` relies on PyPI, not on these signatures.
- A package or a pip installation of 0.1.98 or older does not replace itself: its user downloads the new file
  once. A pip installation of 0.1.94 or older is the package `sandbox-ai` (`sandbox_ai-0.1.94-py3-none-any.whl`),
  which the wheel of `sandbox-ai-app` is never installed over: `pip uninstall sandbox-ai`, then `pip install
  sandbox-ai-app`.
- The macOS app replaces `sandbox-ai.app` in place: what still runs from it loads the new files from then on, so
  an update there waits while a sandbox or session runs (`--while-running` goes ahead) and the app is restarted
  after it.

## The package guard's copy of the known malicious packages

Not a file of a release: a release of its own, tag `osv-malicious`, replaced whenever a newer copy is made (daily
is right: new malware is listed every day). `sandbox-ai packages sync` and `sandbox-ai update` fetch it from
`SOURCE/releases/download/osv-malicious/` and take it only when a release key signed it (the keys of
`sandbox/release-signers`, in the namespace `sandbox-ai-osv`: a release's signature is none of a copy's), every list
matches its SHA-256 in the signed `osv-malicious.json`, and it is not older than the copy already there.

```sh
# OSV's dumps of the four ecosystems (each every advisory of the ecosystem; only MAL-... entries are read)
for e in npm PyPI crates.io Go; do curl -fLo "dumps/$e.zip" "https://osv-vulnerabilities.storage.googleapis.com/$e/all.zip"; done
sandbox-ai packages build --from dumps --out osv-out --sign ~/keys/sandbox-ai-release   # where the release key is
# osv-out: osv-malicious.json, osv-malicious.json.sig, osv-malicious-{npm,pypi,crates,go}.list.gz
sandbox-ai packages sync --from osv-out      # check it as every installation will, before it is published
# then replace the files of the release "osv-malicious" with them (Gitea: any day; GitHub: as its releases)
```

Until that release exists, `sandbox-ai packages sync` says that no copy is published and names
`sandbox-ai packages sync --osv` (the copy built on the user's machine from the same dumps, unsigned and shown so).
The signing key never comes near CI, so this is a maintainer's step (or a timer on the machine that holds the key).

## GitHub and PyPI

The repository runs `.github/workflows/`:

- `ci.yml`: tests on Python 3.9 to 3.13, builds the package and runs every command from the wheel.
- `e2e.yml`, on a push to the branch `e2e` or started by hand: builds the images and runs the live tests
  with real sandboxes, rootless Podman and Bubblewrap with Firejail.
- `release.yml`, for a pushed tag `vX.Y.Z`: tests, checks that the tag matches
  `__version__`, builds `dist/` (with `sandbox-ai.pyz`), the AppImages
  (`ubuntu-24.04`, `ubuntu-24.04-arm`) and the macOS disk images (`macos-15`,
  `macos-15-intel`: `macos-13` is retired), checks each package end to end
  (the `.pyz` on Python 3.9 and 3.13), writes `SHA256SUMS` over all of them,
  stops unless every file CI builds is there, listed and works
  (`scripts/release_check.py --dir dist --ci`),
  creates the GitHub release with all files (only while `sandbox/release-signers`
  lists no key: a signed release is published by the maintainer, see the checklist) and
  uploads wheel and sdist (`sandbox_ai_app-X.Y.Z*`, nothing else) to PyPI by
  trusted publishing. On Gitea/Forgejo Actions the same workflow publishes with
  `publish_release.py` and the secret `RELEASE_TOKEN`; PyPI is skipped there.

PyPI: the project is **`sandbox-ai-app`** (the name `sandbox-ai` there is not
ours: installing that name from PyPI would install someone else's code). One package:
the command line `sandbox-ai` with the web interface, and the desktop app
`sandbox-ai-app` whose PySide6 comes with the extra `[gui]` (exact pins in
`pyproject.toml`, each at least 14 days old). Its trusted publisher on PyPI
(project sandbox-ai-app > Publishing) must read exactly: owner `c0decave`,
repository `sandbox-ai`, workflow `release.yml`, environment `pypi`; the GitHub
repository needs the environment `pypi` too.

`DEFAULT_SOURCE` in `install.sh` and `[project.urls]` in `pyproject.toml` point
to `https://github.com/c0decave/sandbox-ai`.

Before you push or tag a release, run the privacy check on what would be
published: `sandbox-ai privacy-check` (with `--rules-dir` for your
own rules), see the README section "Privacy check
before publishing".
