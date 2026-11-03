#!/usr/bin/env python3
"""Raise the version of sandbox-ai (__version__ in sandbox/agent_sandbox.py).

  scripts/bump_version.py            next patch: 0.1.17 -> 0.1.18; after 0.1.250 comes 0.2.0
  scripts/bump_version.py --minor    next minor: 0.1.17 -> 0.2.0
  scripts/bump_version.py --major    next major: 0.4.3 -> 1.0.0
  scripts/bump_version.py --set X.Y.Z
  scripts/bump_version.py --show     print the current version, change nothing

Prints the new version on stdout.
"""
import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "sandbox" / "agent_sandbox.py"
PATCH_LIMIT = 250
LINE = re.compile(r'^__version__ = "([^"]+)"$', re.M)
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse(text):
    match = SEMVER.fullmatch(text)
    if not match:
        sys.exit(f"bump_version: not a plain X.Y.Z version: {text!r}")
    return tuple(int(part) for part in match.groups())


def next_version(current, part="patch"):
    major, minor, patch = parse(current)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor" or patch >= PATCH_LIMIT:
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--minor", action="store_const", const="minor", dest="part")
    group.add_argument("--major", action="store_const", const="major", dest="part")
    group.add_argument("--set", metavar="X.Y.Z")
    group.add_argument("--show", action="store_true")
    parser.add_argument("--file", type=Path, default=SOURCE, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    text = args.file.read_text()
    match = LINE.search(text)
    if not match:
        sys.exit(f"bump_version: no __version__ line in {args.file}")
    current = match[1]
    if args.show:
        print(current)
        return 0
    if args.set:
        parse(args.set)
        new = args.set
    else:
        new = next_version(current, args.part or "patch")
    args.file.write_text(text[:match.start(1)] + new + text[match.end(1):])
    print(new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
