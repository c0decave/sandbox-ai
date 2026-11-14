#!/usr/bin/env python3
"""Make sandbox/Containerfile.cairn.lock: the hash-pinned Python dependencies of the reviewed Cairn commit.

  scripts/cairn_lock.py SOURCE [--out FILE]

SOURCE is Cairn's repository at the reviewed commit (sandbox/cairn.py: COMMIT; docs/CAIRN_REVIEW.md). Upstream's own
cairn/uv.lock decides every version and hash: `uv export --frozen` writes it out for Cairn's runtime dependencies (no
dev group), and only the requirements that apply to Linux (x86_64 and aarch64) on CPython 3.12 are kept, so the
image's `uv pip install --require-hashes` never meets one it cannot hash (pywin32, colorama). Upstream's lock names a
PyPI mirror; the hashes are the files' own, the same on pypi.org, where the image installs from. Needs uv and the
packaging module; reads SOURCE, never runs code from it (the filters are hermes_lock.py's).
"""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "sandbox" / "Containerfile.cairn.lock"
PYTHON = "3.12"
HEADER = """\
# Cairn's Python dependencies, hash-pinned: the image installs exactly these
# (uv pip install --require-hashes) from pypi.org. Made by scripts/cairn_lock.py from upstream's
# cairn/uv.lock at the reviewed commit (sandbox/cairn.py: COMMIT; docs/CAIRN_REVIEW.md), Linux on
# CPython 3.12 only. Do not edit by hand: a new commit is reviewed, then this is made anew.
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


def export(source):
    command = ["uv", "export", "--project", str(Path(source) / "cairn"), "--frozen", "--no-dev", "--no-emit-project",
               "--format", "requirements-txt", "--no-header", "--no-annotate"]
    return subprocess.run(command, check=True, capture_output=True, text=True,
                          env={"UV_NO_CONFIG": "1", "PATH": "/usr/bin:/bin:/usr/local/bin"}).stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="Cairn's repository at the reviewed commit")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    args.out.write_text(hermes_lock().keep(export(args.source)))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
