#!/usr/bin/env python3
"""Make sandbox/Containerfile.nanobot.lock: the hash-pinned Python dependencies of the reviewed nanobot release.

  scripts/nanobot_lock.py [--out FILE]

nanobot (HKUDS/nanobot, PyPI nanobot-ai) publishes no lock file of its own, so uv resolves the reviewed release
(sandbox/nanobot.py: VERSION; docs/NANOBOT_REVIEW.md) and the extra packages the image takes (EXTRA: the Telegram
channel --nanobot-enable gateway:telegram turns on) from pypi.org, with every file published on or before CUTOFF
(sandbox-ai's rule: third-party releases at least 14 days old when they are taken in), and writes each requirement
with the SHA-256 of every file PyPI has for it. Only the requirements that apply to Linux (x86_64 and aarch64) on
CPython 3.12 are kept (the filters are hermes_lock.py's), so the image's `uv pip install --require-hashes` never
meets one it cannot hash. Needs uv and the packaging module; runs no code of nanobot's.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "sandbox" / "Containerfile.nanobot.lock"
PYTHON = "3.12"
VERSION = "0.4.0"  # sandbox/nanobot.py: VERSION
# The newest day a file may be published: 14 days before the release was taken in (2026-11-14).
CUTOFF = "2026-10-31T00:00:00Z"
# Beside nanobot itself: the Telegram channel's packages (nanobot/channels/telegram/manifest.py: dependencies),
# which nanobot would otherwise install at run time with pip.
EXTRA = ("python-telegram-bot[socks,webhooks]>=22.6,<23.0", "socksio>=1.0.0,<2.0.0",
         "python-socks[asyncio]>=2.8.0,<3.0.0")
HEADER = f"""\
# nanobot's Python dependencies, hash-pinned: the image installs exactly these
# (uv pip install --require-hashes) from pypi.org. Made by scripts/nanobot_lock.py: nanobot-ai {VERSION}
# (sandbox/nanobot.py: VERSION; docs/NANOBOT_REVIEW.md) and its Telegram channel's packages, resolved by uv
# from files published on or before {CUTOFF[:10]}, Linux on CPython {PYTHON} only. Do not edit by hand: a
# new release is reviewed, then this is made anew.
"""


def hermes_lock():
    """scripts/hermes_lock.py: the requirement filters (blocks, applies, keep), shared."""
    spec = importlib.util.spec_from_file_location("hermes_lock", Path(__file__).resolve().parent / "hermes_lock.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.HEADER = HEADER
    module.PLATFORMS = [{"python_version": PYTHON, "python_full_version": PYTHON + ".0",
                         "implementation_name": "cpython",
                         "platform_python_implementation": "CPython", "sys_platform": "linux", "os_name": "posix",
                         "platform_system": "Linux", "platform_machine": machine, "platform_release": "6.12.0"}
                        for machine in ("x86_64", "aarch64")]
    return module


def compile_requirements():
    """uv's resolution of nanobot-ai==VERSION and EXTRA, every requirement with its hashes."""
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "requirements.in"
        source.write_text("\n".join([f"nanobot-ai=={VERSION}", *EXTRA]) + "\n")
        command = ["uv", "pip", "compile", str(source), "--universal", "--generate-hashes",
                   "--python-version", PYTHON, "--exclude-newer", CUTOFF, "--index-url", "https://pypi.org/simple",
                   "--no-header", "--no-annotate", "--quiet"]
        return subprocess.run(command, check=True, capture_output=True, text=True,
                              env={**os.environ, "UV_NO_CONFIG": "1",
                                   "UV_CACHE_DIR": str(Path(directory) / "cache")}).stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    text = hermes_lock().keep(compile_requirements())
    if f"nanobot-ai=={VERSION}" not in text:
        raise SystemExit(f"nanobot-ai=={VERSION} is not in the resolution")
    args.out.write_text(text)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
