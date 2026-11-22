# Third-party components shipped with sandbox-ai

sandbox-ai itself is MIT-licensed (LICENSE). These parts of the repository and of every installation come from
others, unchanged, under their own licences. Each built-in kit keeps the upstream licence text (`LICENSE`), a
`NOTICE` and `kit.json` (repository, commit, every file's SHA-256 and git blob id); the security review of all of
them is [docs/skills-review.md](docs/skills-review.md).

## Skills (sandbox/builtin/kits)

| Kit | Upstream | Commit | Licence | Copyright / notes |
|---|---|---|---|---|
| hallmark | https://github.com/Nutlope/hallmark (`skills/hallmark`) | `8f6969f8922caabde9de1784959eae11ddf68073` | MIT | Copyright (c) 2026 Hallmark contributors; `scripts/photos.json` lists Wikimedia Commons photos (CC0, public domain, CC BY, CC BY-SA), credited where a page uses one |
| superpowers | https://github.com/obra/superpowers (`skills/`) | `2ce8aa15d05bd57213e6f746f953aafb7ded7a8b` (v7.0.1) | MIT | Copyright (c) 2025 Jesse Vincent |
| playwright-skill | https://github.com/lackeyjb/playwright-skill (`skills/playwright-skill`) | `dd47a6a023e249eb1b36e9e943eab89d0900865d` | MIT | Copyright (c) 2025 lackeyjb |
| hyperframes | https://github.com/heygen-com/hyperframes (`skills/`, 7 skills) | `cfa653af4d0f95d751286fb25c8d38bdb56ef663` | Apache-2.0 | HeyGen; no NOTICE file upstream. Bundled with the skills: fonts under the SIL Open Font License 1.1 (EB Garamond, Inter, JetBrains Mono; their OFL texts beside them) and sound effects from Pixabay under the Pixabay Content License (`media-use/audio/assets/sfx/CREDITS.md`) |
| security-audit | https://github.com/cloudflare/security-audit-skill (`skills/security-audit`) | `c1c8a8c1471069fb0e188eeaff69b8e8db6564a8` | MIT | Copyright (c) 2025-2026 Cloudflare, Inc. |
| design-system | https://github.com/anthropics/knowledge-work-plugins (`design/skills/design-system`) | `2574e29377c7173d0cecffef29a6ecbd7f8ccb5d` | Apache-2.0 | Anthropic; no NOTICE file upstream |
| ffuf | https://github.com/jthack/ffuf_claude_skill (`ffuf-skill`) | `50fd4af4d8bd60cfc48fd84eda1720fbd718857d` | MIT, as stated in the upstream README (no LICENSE file upstream; the README is kept as `UPSTREAM-README.md`) | jthack |
| tdd | https://github.com/mattpocock/skills (`skills/engineering/tdd`) | `59db609eb1e18aaf59b63284d0e52255aff0c63c` | MIT | Copyright (c) 2026 Matt Pocock |

Not shipped: https://github.com/chrisvoncsefalvay/claude-d3js-skill has no licence, so it may not be redistributed.

The kit `hallmark-hook` and every kit's `README.md`, `NOTICE`, `kit.json` and agent markers are sandbox-ai's own.

## npm lockfiles of the image recipes (sandbox/recipes)

Only the lockfiles are part of sandbox-ai; the packages are downloaded from registry.npmjs.org when you build the
image variant (`sandbox-ai images recipe hyperframes|playwright`), each checked against the SHA-512 in the lockfile.

- `recipes/hyperframes`: hyperframes 0.8.214 (Apache-2.0, HeyGen) and its 125 packages: MIT 81, Apache-2.0 20,
  LGPL-3.0-or-later 10 (sharp's prebuilt libvips binaries), ISC 6, Apache-2.0 AND LGPL-3.0-or-later 3,
  BSD-3-Clause 2, CC0-1.0 1, 0BSD 1, Apache-2.0 AND LGPL-3.0-or-later AND MIT 1 (the `license` of every package is
  in the lockfile).
- `recipes/playwright`: playwright and playwright-core 1.64.1 (Apache-2.0, Microsoft).

## The Android SDK of the image recipe android-dev (sandbox/recipes/android-dev)

None of the Android SDK is part of sandbox-ai: `install.sh` downloads its packages from dl.google.com when you build
the image variant (`sandbox-ai images recipe android-dev`), each checked against the size and checksums in
`sdk.lock`; by building it you accept the Android Software Development Kit License Agreement
(https://developer.android.com/studio/terms), which they are published under. Gradle 9.8.2 (Apache-2.0, Gradle Inc.)
and the Kotlin compiler 2.4.21 (Apache-2.0, JetBrains) are downloaded from their GitHub releases the same way.

- `recipes/android-dev/package-xml/*.xml`: the `package.xml` sdkmanager writes beside a package it installs, made by
  `scripts/android_dev_lock.py` from Google's SDK repository as nixpkgs keeps it
  (`pkgs/development/mobile/androidenv/repo.json`, MIT; the format follows its `deploy-androidpackages.nix`). Each
  carries the text of the Android Software Development Kit License Agreement (Google LLC), as sdkmanager's own does.
- `recipes/android-dev/android-sdk-license`: the acceptance sdkmanager records for that license (the SHA-1 of its text).

## Other

- `sandbox/marketplace`: sandbox-ai's own Claude plugin marketplace; its entries `egm` (google/elephant-goldfish-model,
  Apache-2.0) and `superpowers` (obra/superpowers, MIT) are fetched from GitHub at their pinned commits only when you
  install them with `sandbox-ai plugins claude`.
- `sandbox/public_suffix_list.dat`: the Public Suffix List (Mozilla Public License 2.0).
- The package guard's lists of known malicious packages (`sandbox-ai packages sync`, kept in
  `~/.local/share/sandbox-ai/osv-malicious`, never part of a release file): made from the `MAL-...` advisories of the
  OSV database (https://osv.dev, its dumps at `osv-vulnerabilities.storage.googleapis.com`), which come from the
  OpenSSF Malicious Packages repository (https://github.com/ossf/malicious-packages, Apache-2.0). A list holds each
  advisory's id, the package's name and its versions; the advisory itself is at `https://osv.dev/vulnerability/ID`.
