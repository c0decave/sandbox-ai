# Releasing sandbox-ai

The version lives in one place: `__version__` in `sandbox/agent_sandbox.py`.
Raise it with `scripts/bump_version.py` for every change you install or ship:
the patch counts up to 250, then the minor goes up (`0.1.250` → `0.2.0`).
`--minor`, `--major`, `--set X.Y.Z` and `--show` cover the rest.

A release consists of

| File | Used by |
|---|---|
| `sandbox-ai-bundle-X.Y.Z.tar.gz` | `install.sh` (the whole tree, reproducible) |
| `sandbox_ai-X.Y.Z-py3-none-any.whl`, `sandbox_ai-X.Y.Z.tar.gz` | PyPI (`pip install sandbox-ai`) |
| `install.sh` | first installs (`curl … | bash`); carries the release keys of `sandbox/release-signers` |
| `sandbox-ai.pyz` | the command line in one file (`python3 sandbox-ai.pyz …`); made by `make_dist.py` from the bundle |
| `sandbox-ai-x86_64.AppImage`, `sandbox-ai-aarch64.AppImage` | the app with sandbox-ai, Python and PySide6 inside, for Linux; built by `release.yml` |
| `sandbox-ai-arm64.dmg`, `sandbox-ai-x86_64.dmg` | `sandbox-ai.app` for macOS (not notarised); built by `release.yml` |
| `SHA256SUMS` | `install.sh` verifies the bundle with it; it lists every file above |
| `SHA256SUMS.sig` | `install.sh` verifies `SHA256SUMS` with it: an SSH signature by a release key |

The file names carry no version, so `https://github.com/c0decave/sandbox-ai/releases/latest/download/NAME`
always names the newest (the README's install commands use that).

## Steps

```bash
# 1. Bump the version, commit, tag
v=$(python3 scripts/bump_version.py)
python3 -m pytest -q tests
git commit -am "Release $v"
git tag "v$v"
git push origin main "v$v"

# 2. Build dist/ and sign SHA256SUMS (needs build and setuptools, as CI pins them:
#    python3 -m pip install --require-hashes --only-binary :all: -r packaging/requirements-ci.txt)
python3 scripts/make_dist.py --no-isolation --sign ~/.config/sandbox-ai/release-key

# 3. Publish the release with its files (a token that may write releases);
#    SANDBOX_AI_SOURCE names your own Gitea/Forgejo repository, if you publish there
export SANDBOX_AI_TOKEN=...
python3 scripts/publish_release.py --source "${SANDBOX_AI_SOURCE:-https://github.com/c0decave/sandbox-ai}" dist/*
```

Without a key in `sandbox/release-signers` (see below), leave out `--sign`.

## The ready-made packages

| File | Built by | Where |
|---|---|---|
| `sandbox-ai.pyz` | `scripts/make_pyz.py` (called by `make_dist.py`; `--skip-pyz` leaves it out) | anywhere |
| `sandbox-ai-ARCH.AppImage` | `scripts/build_appimage.sh --bundle dist/sandbox-ai-bundle-X.Y.Z.tar.gz` | Linux of that architecture |
| `sandbox-ai-ARCH.dmg` | `scripts/build_macos_app.sh --bundle dist/sandbox-ai-bundle-X.Y.Z.tar.gz` | macOS of that architecture |

All three are made from the bundle, so they carry exactly the tree `install.sh` would install, with the app
(`projects/sandbox-ai-app`: build `dist/` from a checkout that has it; `release.yml` checks out
`c0decave/sandbox-ai-app` at the repository variable `SANDBOX_AI_APP_REF`, default `main`). Each package has a
`PACKAGING` file beside `sandbox/`, so `sandbox-ai update` points to the download instead of running
`install.sh`. `scripts/e2e_package.sh KIND FILE` checks a package the way a user starts it (version, presets,
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
`make_dist.py --no-isolation` builds with that setuptools), and a tag build stops unless `SANDBOX_AI_APP_REF` is
the app's commit (40 hex digits). Every third-party download is pinned by release, exact file name and SHA-256: python-build-standalone,
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
contain its bundle, so sign that bundle, not one rebuilt here):

```bash
gh run download RUN_ID -n dist -D dist                              # the run of the tag's release.yml
gh run download RUN_ID -p 'appimage-*' -p 'macos-*' -D packages
python3 scripts/make_dist.py --sums-only --out dist --add packages/*/* --sign ~/.config/sandbox-ai/release-key
python3 scripts/publish_release.py --source https://github.com/c0decave/sandbox-ai dist/*
```

`--sums-only` keeps `dist/` as it is, `--add` copies the packages in, and
`SHA256SUMS` (with its signature) then covers every file. `make_dist.py`
builds the same bundle and `.pyz` from the same commit and app checkout
(reproducible), so you can compare them with CI's before you sign. Set the
repository variable `SANDBOX_AI_APP_REF` to the app's tag or commit for a
release: its default `main` moves. Before that, a key stored as a CI secret would make the signature
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
- pip installations (`pip install -U sandbox-ai`) rely on PyPI, not on these
  signatures.

## GitHub and PyPI

The repository runs `.github/workflows/`:

- `ci.yml`: tests on Python 3.9 to 3.13, builds the package and runs every command from the wheel.
- `release.yml`, for a pushed tag `vX.Y.Z`: tests, checks that the tag matches
  `__version__`, builds `dist/` (with `sandbox-ai.pyz`), the AppImages
  (`ubuntu-24.04`, `ubuntu-24.04-arm`) and the macOS disk images (`macos-14`,
  `macos-15-intel`: `macos-13` is retired), checks each package end to end
  (the `.pyz` on Python 3.9 and 3.13), writes `SHA256SUMS` over all of them,
  creates the GitHub release with all files and
  uploads wheel and sdist to PyPI by trusted publishing (register the
  repository, the workflow `release.yml` and the environment `pypi` on PyPI
  first). On Gitea/Forgejo Actions the same workflow publishes with
  `publish_release.py` and the secret `RELEASE_TOKEN`; PyPI is skipped there.

`DEFAULT_SOURCE` in `install.sh` and `[project.urls]` in `pyproject.toml` point
to `https://github.com/c0decave/sandbox-ai`.

Before you push or tag a release, run the privacy check on what would be
published: `sandbox-ai privacy-check` (with `--rules-dir` for your
own rules), see the README section "Privacy check
before publishing".
