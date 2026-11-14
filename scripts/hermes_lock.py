#!/usr/bin/env python3
"""Make sandbox/Containerfile.hermes.lock: the hash-pinned Python dependencies of the reviewed Hermes release.

  scripts/hermes_lock.py SOURCE [--out FILE]

SOURCE is Hermes' source at the reviewed tag (hermes.py: VERSION and COMMIT; docs/HERMES_REVIEW.md). Upstream's own
uv.lock decides every version and hash: `uv export --frozen` writes it out for Hermes' core and the extras the image
takes (EXTRAS), and only the requirements that apply to Linux (x86_64 and aarch64) on CPython 3.14 are kept, so the
image's `uv pip install --require-hashes` never meets one it cannot hash (a Windows-only package, Android's psutil from
git). Needs uv and the packaging module; reads SOURCE, never runs code from it.
"""
import argparse
from pathlib import Path
import subprocess
import sys

from packaging.markers import Marker

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "sandbox" / "Containerfile.hermes.lock"
# The extras installed beside the core: MCP (--mcp), Anthropic's API, and the messaging platforms --hermes-enable
# can turn on (gateway:telegram, gateway:discord, gateway:slack). Nothing that runs the terminal elsewhere (modal,
# daytona, vercel), no voice or local models.
EXTRAS = ("mcp", "anthropic", "telegram", "discord", "slack")
PLATFORMS = [{"python_version": "3.14", "python_full_version": "3.14.0", "implementation_name": "cpython",
              "platform_python_implementation": "CPython", "sys_platform": "linux", "os_name": "posix",
              "platform_system": "Linux", "platform_machine": machine, "platform_release": "6.12.0"}
             for machine in ("x86_64", "aarch64")]
HEADER = """\
# Hermes Agent's Python dependencies, hash-pinned: the image installs exactly these
# (uv pip install --require-hashes). Made by scripts/hermes_lock.py from upstream's uv.lock at
# the reviewed tag (sandbox/hermes.py: VERSION, COMMIT; docs/HERMES_REVIEW.md), Linux on
# CPython 3.14 only. Do not edit by hand: a new release is reviewed, then this is made anew.
"""


def blocks(text):
    """The requirement blocks of a requirements file: each requirement line with its continuation lines."""
    found, current = [], []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")) and current:
            current.append(line)
            continue
        if current:
            found.append(current)
        current = [line]
    if current:
        found.append(current)
    return found


def applies(block):
    """True if the requirement BLOCK holds on one of the PLATFORMS."""
    requirement = block[0].rstrip(" \\")
    if ";" not in requirement:
        return True
    marker = Marker(requirement.split(";", 1)[1].strip())
    return any(marker.evaluate(environment) for environment in PLATFORMS)


def keep(text):
    """TEXT (uv export's requirements) with only the blocks that apply, every one of them hashed."""
    kept = []
    for block in blocks(text):
        if not applies(block):
            continue
        if not any("--hash=sha256:" in line for line in block):
            raise ValueError(f"no hash for {block[0]!r}")
        lines = list(block)
        lines[-1] = lines[-1].rstrip(" \\")
        kept.append("\n".join(lines))
    return HEADER + "\n".join(kept) + "\n"


def export(source):
    command = ["uv", "export", "--project", str(source), "--frozen", "--no-dev", "--no-emit-project",
               "--format", "requirements-txt", "--no-header", "--no-annotate",
               *(part for extra in EXTRAS for part in ("--extra", extra))]
    return subprocess.run(command, check=True, capture_output=True, text=True, env={"UV_NO_CONFIG": "1",
                                                                                     "PATH": "/usr/bin:/bin:/usr/local/bin"}).stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="Hermes' source at the reviewed tag")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    args.out.write_text(keep(export(args.source)))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
