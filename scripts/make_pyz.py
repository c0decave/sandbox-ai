#!/usr/bin/env python3
"""Build sandbox-ai.pyz, the single-file command line, and the manifests of the app packages.

  scripts/make_pyz.py --bundle dist/sandbox-ai-bundle-X.Y.Z.tar.gz --out dist/sandbox-ai.pyz
  scripts/make_pyz.py --manifest --tree APPDIR/usr/lib/sandbox-ai/tree --kind appimage --home sandbox-ai \\
                      --python python/bin/python3 --out APPDIR/usr/lib/sandbox-ai/manifest.json

The .pyz is a zip archive behind a `#!/bin/sh` line that runs `python3 -I` on it: scripts/dist_boot.py as its
__main__.py, manifest.json (every file of the tree with SHA-256, size and mode; the version; what to run) and the tree under
tree/ (the bundle as make_dist.py builds it, plus PACKAGING, which tells sandbox-ai how it was installed). On
its first start it extracts the tree into the cache and runs sandbox/main.py from there (see dist_boot.py).

Reproducible: entries sorted and stored (deflate differs between zlib builds), fixed time stamps and modes
(SOURCE_DATE_EPOCH, default: the bundle's own time), so one bundle always gives the same .pyz, on any machine.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BOOT = ROOT / "scripts" / "dist_boot.py"
# A shell line before the zip archive (Python finds the archive from its end): `./sandbox-ai.pyz` runs python3
# isolated (-I), which a `#!/usr/bin/env python3` line cannot pass on every system; PYTHONPATH, PYTHONSTARTUP and
# the user's site never reach the start.
SHEBANG = b'#!/bin/sh\n# sandbox-ai.pyz: python3 -I sandbox-ai.pyz ARGS (Python 3.9+)\nexec python3 -I "$0" "$@"\n'

ZIP_EPOCH = 315532800  # 1980-01-01, the earliest time a zip archive can hold
VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.M)


def fail(message):
    sys.exit(f"make_pyz: {message}")


def bundle_tree(path):
    """({name: (data, mode)}, mtime) of a sandbox-ai bundle .tar.gz, below its one top folder."""
    files, top, mtime = {}, None, None
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            parts = member.name.split("/")
            if top is None:
                top = parts[0]
            if parts[0] != top or any(part in ("", ".", "..") for part in parts) or member.name.startswith("/"):
                fail(f"{path}: unsafe or second top entry {member.name!r}")
            if member.isdir():
                continue
            if not member.isfile():
                fail(f"{path}: {member.name} is neither a file nor a folder")
            name = "/".join(parts[1:])
            files[name] = (tar.extractfile(member).read(), 0o755 if member.mode & 0o111 else 0o644)
            mtime = member.mtime if mtime is None else max(mtime, member.mtime)
    if not files:
        fail(f"{path}: empty bundle")
    return files, mtime or 0


def dir_tree(top):
    """({name: (data, mode)}, [(name, target)]) of a folder; links must stay inside it."""
    top = Path(top)
    files, links = {}, []
    for folder, dirs, names in os.walk(top, followlinks=False):
        for name in sorted(dirs + names):
            path = Path(folder) / name
            rel = path.relative_to(top).as_posix()
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                resolved = os.path.normpath(os.path.join(os.path.dirname(rel), target))
                if os.path.isabs(target) or resolved == ".." or resolved.startswith("../"):
                    fail(f"{path} links outside the tree ({target})")
                links.append((rel, target))
            elif stat.S_ISREG(info.st_mode):
                files[rel] = (path.read_bytes(), 0o755 if info.st_mode & 0o111 else 0o644)
            elif not stat.S_ISDIR(info.st_mode):
                fail(f"{path} is neither a file, a folder nor a link")
        dirs[:] = [d for d in dirs if not (Path(folder) / d).is_symlink()]
    return files, sorted(links)


def tree_version(files, home=""):
    name = f"{home}/sandbox/agent_sandbox.py" if home else "sandbox/agent_sandbox.py"
    if name not in files:
        fail(f"no {name} in the tree")
    match = VERSION.search(files[name][0].decode("utf-8", "replace"))
    if not match:
        fail(f"no __version__ in {name}")
    return match[1]


def manifest(files, links=(), kind="pyz", home="", python=None, compile_dirs=("sandbox",), version=None):
    """manifest.json's bytes: what dist_boot.py checks and runs."""
    data = {"format": 1, "kind": kind, "name": "sandbox-ai", "version": version or tree_version(files, home),
            "home": home, "python": python, "compile": list(compile_dirs),
            "files": [[name, hashlib.sha256(files[name][0]).hexdigest(), len(files[name][0]), files[name][1]]
                      for name in sorted(files)],
            "links": [list(link) for link in sorted(links)]}
    return (json.dumps(data, indent=0, sort_keys=True) + "\n").encode()


def zip_info(name, epoch, mode, compress=zipfile.ZIP_STORED):
    info = zipfile.ZipInfo(name, time.gmtime(max(epoch, ZIP_EPOCH))[:6])
    info.create_system = 3  # unix: the mode below counts
    info.external_attr = ((0o100000 | mode) << 16)
    info.compress_type = compress
    return info


def write_pyz(out, files, manifest_bytes, epoch, boot=None):
    """The .pyz at OUT (written beside it, then renamed): the boot, the manifest, the tree; reproducible."""
    boot = BOOT.read_bytes() if boot is None else boot
    out = Path(out)
    tmp = out.with_name(f".{out.name}.tmp")
    with open(tmp, "wb") as stream:
        stream.write(SHEBANG)
        with zipfile.ZipFile(stream, "w") as archive:
            # Every entry stored, not deflated: deflate's output differs between zlib and zlib-ng (distributions
            # ship either), the .pyz must not. The manifest second: every start reads it directly
            # (dist_boot.stored_manifest), without zipfile.
            archive.writestr(zip_info("__main__.py", epoch, 0o644), boot)
            archive.writestr(zip_info("manifest.json", epoch, 0o644), manifest_bytes)
            for name in sorted(files):
                data, mode = files[name]
                archive.writestr(zip_info(f"tree/{name}", epoch, mode), data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, out)
    return out


def build_pyz(bundle, out, epoch=None):
    files, mtime = bundle_tree(bundle)
    # Not packaging/: build inputs only (pins, requirements): on macOS's case-insensitive file system packaging/ and the marker PACKAGING are one name.
    files = {name: entry for name, entry in files.items() if not name.startswith("packaging/")}
    files["PACKAGING"] = (b"pyz\n", 0o644)  # sandbox-ai update: "download the new .pyz", not install.sh
    if epoch is None:
        epoch = int(os.environ["SOURCE_DATE_EPOCH"]) if os.environ.get("SOURCE_DATE_EPOCH") else mtime
    return write_pyz(out, files, manifest(files), epoch)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build sandbox-ai.pyz or a package manifest")
    parser.add_argument("--bundle", type=Path, help="sandbox-ai-bundle-X.Y.Z.tar.gz (made by make_dist.py)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--manifest", action="store_true", help="Only write the manifest of --tree")
    parser.add_argument("--tree", type=Path)
    parser.add_argument("--kind", default="pyz", choices=("pyz", "appimage"))
    parser.add_argument("--home", default="", help="Where sandbox/ lies in the tree")
    parser.add_argument("--python", help="The tree's own Python, relative to the tree")
    parser.add_argument("--compile", action="append", default=None,
                        help="Folders the first start compiles (default for the .pyz: sandbox)")
    a = parser.parse_args(argv)
    if a.manifest:
        if not a.tree or not a.tree.is_dir():
            fail("--manifest needs --tree DIR")
        files, links = dir_tree(a.tree)
        a.out.write_bytes(manifest(files, links, a.kind, a.home, a.python, a.compile or ()))
        print(f"make_pyz: {a.out} ({len(files)} files, {len(links)} links)", file=sys.stderr)
        return 0
    if not a.bundle or not a.bundle.is_file():
        fail("--bundle FILE is required (scripts/make_dist.py builds it)")
    out = build_pyz(a.bundle, a.out)
    print(f"make_pyz: {out} ({out.stat().st_size // 1024} KiB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
