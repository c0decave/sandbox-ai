#!/usr/bin/env python3
"""Build the release files of sandbox-ai into dist/.

  sandbox-ai-bundle-X.Y.Z.tar.gz   the tree install.sh installs (reproducible: sorted, fixed owner and time)
  sandbox_ai-X.Y.Z.tar.gz, *.whl   the PyPI package (python -m build; needs the "build" package)
  sandbox-ai.pyz                   the command line in one file (python3 sandbox-ai.pyz ...; scripts/make_pyz.py)
  install.sh                       the installer, for curl ... | bash style downloads, with the release keys of
                                   sandbox/release-signers embedded (they check the very first install)
  SHA256SUMS                       checksums of all of the above; install.sh verifies the bundle with it
  SHA256SUMS.sig                   with --sign KEY: the SSH signature over SHA256SUMS (namespace sandbox-ai-release)

Usage: scripts/make_dist.py [--out DIR] [--allow-dirty] [--skip-python] [--skip-pyz] [--no-isolation] [--add FILE ...]
                             [--sign KEY]
       scripts/make_dist.py --sums-only [--out DIR] [--add FILE ...] [--sign KEY]

--add copies further release files into DIR before the checksums are made: the AppImages and disk images the
release workflow builds (RELEASING.md). --sums-only keeps DIR as it is and only (re)writes SHA256SUMS (and with
--sign its signature) over every file in it.

--sign takes a private key file (ssh-keygen asks for its passphrase) or the .pub of a key in ssh-agent or on
a FIDO token. The signature must verify against sandbox/release-signers, or the build stops: an install
that trusts this release's keys could not check it otherwise.
"""
import argparse
import gzip
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
EMBEDDED = re.compile(r"(# BEGIN EMBEDDED RELEASE SIGNERS\n)EMBEDDED_SIGNERS='[^']*'\n(# END EMBEDDED RELEASE SIGNERS\n)")


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release_keys():
    spec = importlib.util.spec_from_file_location("sandbox_release_keys", ROOT / "sandbox" / "release_keys.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def version():
    text = (ROOT / "sandbox" / "agent_sandbox.py").read_text()
    match = re.search(r'^__version__ = "([^"]+)"$', text, re.M)
    if not match:
        sys.exit("make_dist: no __version__ in sandbox/agent_sandbox.py")
    return match[1]


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout


APP = "projects/sandbox-ai-app"  # the graphical front end: its own repository inside the checkout
MCP_CATALOG = "projects/sandbox-ai-mcp/catalog.json"  # the MCP servers' catalog, from that repository
# ... with what makes it usable: its signature, and the Bubblewrap form of its servers with its own.
MCP_CATALOG_FILES = (MCP_CATALOG, MCP_CATALOG + ".sig", "projects/sandbox-ai-mcp/rootfs.json",
                     "projects/sandbox-ai-mcp/rootfs.json.sig")


def release_files():
    """Tracked and new, not ignored files; local tool state (.claude/) never ships. The app's files come from
    its own repository (the checkout's git sees only one directory there)."""
    names = set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0"))
    if (ROOT / APP / "sandbox_ai_app" / "__main__.py").is_file():
        listed = subprocess.run(["git", "-C", str(ROOT / APP), "ls-files", "-z", "--cached", "--others",
                                 "--exclude-standard"], capture_output=True, text=True, check=True).stdout
        names |= {f"{APP}/{n}" for n in listed.split("\0") if n}
    if (ROOT / MCP_CATALOG).is_file():  # read by sandbox-ai mcp before its published URL
        names |= {name for name in MCP_CATALOG_FILES if (ROOT / name).is_file()}
    return sorted(n for n in names if n and not n.startswith((".claude/", "dist/"))
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


def embed_signers(installer, keys):
    """install.sh with the release keys in place of its empty EMBEDDED_SIGNERS: they check a first install."""
    lines = release_keys().normalized(keys).rstrip("\n")
    text, count = EMBEDDED.subn(lambda m: f"{m[1]}EMBEDDED_SIGNERS='{lines}'\n{m[2]}", installer)
    if count != 1:
        sys.exit("make_dist: install.sh has no EMBEDDED RELEASE SIGNERS block")
    return text


def sign(out, key, keys):
    """SHA256SUMS.sig; it must verify against the release keys this release ships."""
    rk = release_keys()
    sums = (out / "SHA256SUMS").read_bytes()
    try:
        signature = rk.sign(sums, key)
    except RuntimeError as exc:
        sys.exit(f"make_dist: {exc}")
    verdict, detail = rk.verify(sums, signature, keys)
    if verdict != "good":
        sys.exit(f"make_dist: {detail}. Add the key to sandbox/release-signers first "
                 "(scripts/release_key.sh show >> sandbox/release-signers) and commit it.")
    (out / "SHA256SUMS.sig").write_bytes(signature)
    return detail


def checksums(out):
    lines = []
    for path in sorted(out.iterdir()):
        if path.name not in ("SHA256SUMS", "SHA256SUMS.sig") and path.is_file():
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
    (out / "SHA256SUMS").write_text("".join(lines))
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build sandbox-ai release files")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--allow-dirty", action="store_true", help="Build from uncommitted changes (local tests)")
    parser.add_argument("--skip-python", action="store_true", help="Only bundle, installer and checksums")
    parser.add_argument("--skip-pyz", action="store_true", help="Leave out sandbox-ai.pyz")
    parser.add_argument("--no-isolation", action="store_true",
                        help="Build the PyPI package with the installed setuptools (CI: hash-pinned, "
                             "packaging/requirements-ci.txt) instead of one python -m build would download")
    parser.add_argument("--add", metavar="FILE", type=Path, nargs="+", default=[],
                        help="Further release files (AppImages, disk images) to copy in before the checksums")
    parser.add_argument("--sums-only", action="store_true",
                        help="Build nothing: (re)write SHA256SUMS over the files in --out (and sign it)")
    parser.add_argument("--sign", metavar="KEY", type=Path,
                        help="Sign SHA256SUMS with this SSH key (a private key, or the .pub of a key in ssh-agent)")
    a = parser.parse_args(argv)
    ver = version()
    try:
        keys = release_keys().load()
    except ValueError as exc:
        sys.exit(f"make_dist: {exc}")
    if a.sign and not (a.sign.is_file() or Path(f"{a.sign}.pub").is_file()):
        sys.exit(f"make_dist: --sign: no such key: {a.sign}")
    for extra in a.add:
        if not extra.is_file() or extra.is_symlink() or extra.name in ("SHA256SUMS", "SHA256SUMS.sig"):
            sys.exit(f"make_dist: --add: not a release file: {extra}")
    if not a.sums_only and any(a.out.resolve() in extra.resolve().parents for extra in a.add):
        sys.exit(f"make_dist: --add: files in {a.out} are removed by the build; use --sums-only to keep them")
    if a.sums_only:
        out = a.out.resolve()
        if not out.is_dir():
            sys.exit(f"make_dist: --sums-only: no folder {out}")
        return finish(out, a, keys, ver)
    dirty = [line for line in git("status", "--porcelain").splitlines() if not line[3:].startswith(".claude/")]
    if dirty and not a.allow_dirty:
        sys.exit("make_dist: uncommitted changes; commit them (or pass --allow-dirty for a local test):\n"
                 + "\n".join(dirty[:20]))
    out = a.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    bundled = bundle(out, ver)
    if not a.skip_pyz:
        load_script("make_pyz").build_pyz(bundled, out / "sandbox-ai.pyz", epoch=source_date())
    (out / "install.sh").write_text(embed_signers((ROOT / "install.sh").read_text(), keys))
    (out / "install.sh").chmod(0o755)
    if not a.skip_python:
        try:
            subprocess.run([sys.executable, "-m", "build", "--sdist", "--wheel", *(["--no-isolation"] if a.no_isolation
                            else []), "--outdir", str(out), str(ROOT)], check=True)
        except (OSError, subprocess.CalledProcessError):
            sys.exit("make_dist: building the PyPI package failed; install the builder with "
                     "`python3 -m pip install build`, or pass --skip-python")
    return finish(out, a, keys, ver)


def finish(out, a, keys, ver):
    """The --add files copied in, SHA256SUMS over everything in OUT, and its signature with --sign."""
    for extra in a.add:
        if extra.resolve() != (out / extra.name).resolve():
            shutil.copyfile(extra, out / extra.name)
            if os.access(extra, os.X_OK):
                (out / extra.name).chmod(0o755)
    stale = out / "SHA256SUMS.sig"
    if stale.exists():  # it signed the SHA256SUMS that is rewritten now
        stale.unlink()
    for line in checksums(out):
        print(line, end="")
    if a.sign:
        print(f"make_dist: signed SHA256SUMS with {sign(out, a.sign, keys)} (SHA256SUMS.sig)", file=sys.stderr)
    elif keys:
        print("make_dist: SHA256SUMS is NOT signed, but sandbox/release-signers lists keys: installations refuse "
              "this release until it carries SHA256SUMS.sig (--sign KEY)", file=sys.stderr)
    print(f"make_dist: sandbox-ai {ver} in {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
