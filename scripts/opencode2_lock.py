#!/usr/bin/env python3
"""Make sandbox/Containerfile.opencode2.lock: the sha512 integrity of each OpenCode 2 binary package the image takes.

  scripts/opencode2_lock.py [--version X.Y.Z] [--out FILE] [--today YYYY-MM-DD]

The packages and the version come from sandbox/opencode2.py (PLATFORMS, VERSION; the review: docs/OPENCODE2_REVIEW.md).
npm's registry gives each package's publication time and integrity; a release younger than 14 days (opencode2.MIN_AGE,
sandbox-ai's rule for third-party releases) is refused with the day it becomes eligible, so a newer release can be
prepared and locked only once it may be pinned. The image (sandbox/Containerfile.opencode2) downloads only the
package of its architecture and checks it against this lock before it unpacks anything. Standard library only.
"""
import argparse
import datetime
import importlib.util
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location("sandbox_ai_opencode2", ROOT / "sandbox" / "opencode2.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def registry_entry(registry, package, version):
    """(published, integrity) of PACKAGE@VERSION on REGISTRY."""
    url = f"{registry}/{urllib.parse.quote(package, safe='@')}"
    with urllib.request.urlopen(url, timeout=60) as response:
        document = json.load(response)
    entry = document.get("versions", {}).get(version)
    if not entry:
        raise ValueError(f"{package}@{version} is not on the registry")
    integrity = entry.get("dist", {}).get("integrity", "")
    if not integrity.startswith("sha512-"):
        raise ValueError(f"{package}@{version} has no sha512 integrity")
    return document["time"][version], integrity


def lock_text(oc2, version, entries):
    """The lock file: a comment, then one line per Debian architecture: INTEGRITY  PACKAGE@VERSION  ARCH."""
    lines = ["# OpenCode 2's binary packages from npm's registry, as the image checks them (sandbox/Containerfile.opencode2):",
             f"# the reviewed release only (sandbox/opencode2.py: VERSION {version}, PLATFORMS; docs/OPENCODE2_REVIEW.md),",
             "# each the registry's sha512 integrity of the package's tarball. Do not edit by hand: a new release is",
             "# reviewed, then this is made anew by scripts/opencode2_lock.py (which refuses a release younger than 14 days)."]
    for arch, (package, integrity) in entries.items():
        lines.append(f"{integrity}  {package}@{version}  {arch}")
    return "\n".join(lines) + "\n"


def parse_lock(text):
    """{arch: (package, version, integrity)} of a lock file's TEXT."""
    found = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        integrity, spec, arch = line.split()
        package, _, version = spec.rpartition("@")
        found[arch] = (package, version, integrity)
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--today", help="the day to apply the 14-day rule for (default: today, UTC)")
    args = parser.parse_args(argv)
    oc2 = module()
    version = args.version or oc2.VERSION
    now = (datetime.datetime.fromisoformat(args.today).replace(tzinfo=datetime.timezone.utc) if args.today
           else datetime.datetime.now(datetime.timezone.utc))
    entries = {}
    for arch, (package, _) in oc2.PLATFORMS.items():
        published, integrity = registry_entry(oc2.REGISTRY, package, version)
        if not oc2.eligible(published, now):
            when = datetime.datetime.fromisoformat(published.replace("Z", "+00:00")) + oc2.MIN_AGE
            print(f"{package}@{version} was published {published}: younger than 14 days; eligible from "
                  f"{when.isoformat(timespec='minutes')}", file=sys.stderr)
            return 1
        entries[arch] = (package, integrity)
    out = args.out or ROOT / "sandbox" / "Containerfile.opencode2.lock"
    out.write_text(lock_text(oc2, version, entries))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
