#!/usr/bin/env python3
"""Build the release files of sandbox-ai into dist/.

  sandbox-ai-bundle-X.Y.Z.tar.gz   the tree install.sh installs (reproducible: sorted, fixed owner and time)
  sandbox_ai-X.Y.Z.tar.gz, *.whl   the PyPI package (python -m build; needs the "build" package)
  install.sh                       the installer, for curl ... | bash style downloads
  SHA256SUMS                       checksums of all of the above; install.sh verifies the bundle with it

Usage: scripts/make_dist.py [--out DIR] [--allow-dirty] [--skip-python]
"""
import argparse
import gzip
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def version():
    text = (ROOT / "sandbox" / "agent_sandbox.py").read_text()
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.M)
    if not match:
        sys.exit("make_dist: no __version__ in sandbox/agent_sandbox.py")
    return match[1]


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout


def release_files():
    """Tracked and new, not ignored files; local tool state (.claude/) never ships."""
    names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    return sorted(n for n in set(names) if n and not n.startswith((".claude/", "dist/"))
                  and (ROOT / n).is_file() and not (ROOT / n).is_symlink())


def source_date():
    if os.environ.get("SOURCE_DATE_EPOCH"):
        return int(os.environ["SOURCE_DATE_EPOCH"])
    try:
        return int(git("log", "-1", "--format=%ct").strip())
    except (subprocess.CalledProcessError, ValueError):
        return 0


def bundle(out, ver):
    path = out / f"sandbox-ai-bundle-{ver}.tar.gz"
    top, mtime = f"sandbox-ai-{ver}", source_date()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        directories = sorted({str(Path(n).parent) for n in release_files()} - {"."})
        for name in ["", *directories]:
            info = tarfile.TarInfo(f"{top}/{name}".rstrip("/"))
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, mtime
            tar.addfile(info)
        for name in release_files():
            data = (ROOT / name).read_bytes()
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size, info.mtime = len(data), mtime
            info.mode = 0o755 if os.access(ROOT / name, os.X_OK) else 0o644
            tar.addfile(info, io.BytesIO(data))
    with open(path, "wb") as stream, gzip.GzipFile(fileobj=stream, mode="wb", mtime=mtime, filename="") as gz:
        gz.write(raw.getvalue())
    return path


def checksums(out):
    lines = []
    for path in sorted(out.iterdir()):
        if path.name != "SHA256SUMS" and path.is_file():
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
    (out / "SHA256SUMS").write_text("".join(lines))
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build sandbox-ai release files")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--allow-dirty", action="store_true", help="Build from uncommitted changes (local tests)")
    parser.add_argument("--skip-python", action="store_true", help="Only bundle, installer and checksums")
    a = parser.parse_args(argv)
    ver = version()
    dirty = [line for line in git("status", "--porcelain").splitlines() if not line[3:].startswith(".claude/")]
    if dirty and not a.allow_dirty:
        sys.exit("make_dist: uncommitted changes; commit them (or pass --allow-dirty for a local test):\n"
                 + "\n".join(dirty[:20]))
    out = a.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    bundle(out, ver)
    shutil.copy2(ROOT / "install.sh", out / "install.sh")
    if not a.skip_python:
        try:
            subprocess.run([sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(out), str(ROOT)],
                           check=True)
        except (OSError, subprocess.CalledProcessError):
            sys.exit("make_dist: building the PyPI package failed; install the builder with "
                     "`python3 -m pip install build`, or pass --skip-python")
    for line in checksums(out):
        print(line, end="")
    print(f"make_dist: sandbox-ai {ver} in {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
