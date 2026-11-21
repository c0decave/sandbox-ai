"""Test releases of sandbox-ai, built the way a real one is: a copy of this checkout as version X.Y.Z that trusts
the tests' own release key (never the real one), scripts/make_dist.py over it (bundle, sandbox-ai.pyz, install.sh
with the keys, the wheel), stand-ins for the packages only CI can build (an AppImage, a macOS disk image: the real
start code - scripts/dist_boot.py, the manifest, PACKAGING - behind a stand-in for the AppImage runtime and for
hdiutil), SHA256SUMS over all of it and its signature. tests/fake_github.py serves them.
"""
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace

REPOSITORY = Path(__file__).resolve().parents[1]
VERSION = re.search(r'^__version__ = "([^"]+)"$', (REPOSITORY / "sandbox/agent_sandbox.py").read_text(), re.M)[1]
VERSION_LINE = re.compile(r'^__version__ = "[^"]+"', re.M)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rk = load(REPOSITORY / "sandbox/release_keys.py", "release_keys_for_release_kit")
assets = load(REPOSITORY / "sandbox/release_assets.py", "release_assets_for_release_kit")


def remove(folder):
    """FOLDER and everything in it, also what a package's start left read-only there (the copies it unpacks into the
    cache: 0500 folders, which rmtree alone leaves behind - and the disk fills up)."""
    folder = Path(folder)
    if folder.is_dir() and not folder.is_symlink():
        subprocess.run(["chmod", "-R", "u+w", "--", str(folder)], capture_output=True, check=False)
    shutil.rmtree(folder, ignore_errors=True)


def later(version=VERSION, steps=1):
    """A version after VERSION (its patch raised)."""
    major, minor, patch = (int(part) for part in version.split(".")[:3])
    return f"{major}.{minor}.{patch + steps}"


def new_key(folder, name="release"):
    """A throwaway ed25519 key: .path (the private key), .line (for sandbox/release-signers), .fingerprint."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(folder / name)], check=True)
    kind, key = (folder / f"{name}.pub").read_text().split()[:2]
    return SimpleNamespace(path=folder / name, kind=kind, key=key, line=rk.line(kind, key),
                           fingerprint=rk.fingerprint(key), name=name)


def tree_copy(target, version=None, keys=(), source=REPOSITORY):
    """The files make_dist.py would take from SOURCE (tracked and new ones, not ignored), as a Git repository of its
    own at TARGET: version VERSION (sandbox-ai's and the app's), sandbox/release-signers listing KEYS only."""
    target = Path(target)
    names = subprocess.run(["git", "-C", str(source), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                           capture_output=True, text=True, check=True).stdout.split("\0")
    for name in filter(None, names):
        path = Path(source) / name
        if path.is_file() or path.is_symlink():
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target / name, follow_symlinks=False)
    set_keys(target, keys)
    if version:
        set_version(target, version)
    commit(target, "copy")
    return target


def set_keys(tree, keys):
    signers = Path(tree) / "sandbox/release-signers"
    comments = "".join(line for line in signers.read_text().splitlines(True) if line.startswith("#"))
    signers.write_text(comments + "".join(f"{key.line} {key.name}\n" for key in keys))


def set_version(tree, version):
    for name in ("sandbox/agent_sandbox.py", "projects/sandbox-ai-app/sandbox_ai_app/__init__.py"):
        path = Path(tree) / name
        if path.is_file():
            path.write_text(VERSION_LINE.sub(f'__version__ = "{version}"', path.read_text(), count=1))


def commit(tree, message):
    git = ["git", "-C", str(tree), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    if not (Path(tree) / ".git").exists():
        subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", message], check=True)


def make_dist(tree, out, *args, python=sys.executable, seconds=600):
    """scripts/make_dist.py of TREE into OUT (ARGS: --skip-python, --sign KEY, ...)."""
    done = subprocess.run([python, str(Path(tree) / "scripts/make_dist.py"), "--no-isolation", "--out", str(out),
                           *map(str, args)], capture_output=True, text=True, timeout=seconds,
                          env={**os.environ, "SOURCE_DATE_EPOCH": "1700000000"})
    assert done.returncode == 0, (done.stdout + done.stderr)[-3000:]
    return Path(out)


def sums_only(tree, out, *args, python=sys.executable):
    """SHA256SUMS (and with --sign KEY its signature) written anew over what OUT holds."""
    done = subprocess.run([python, str(Path(tree) / "scripts/make_dist.py"), "--sums-only", "--out", str(out),
                           *map(str, args)], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, (done.stdout + done.stderr)[-3000:]
    return Path(out)


# -- stand-ins for the packages only CI builds ------------------------------------------------------------------------

# The AppImage runtime's part: it mounts (here: unpacks) the AppDir, sets APPIMAGE and ARGV0 and runs AppRun - here
# boot.py (scripts/dist_boot.py) with python3, as packaging/AppRun does with the Python inside a real one. The AppDir
# is the tar archive after the marker line; --appimage-extract unpacks it into ./squashfs-root, as the real runtime.
APPIMAGE_STUB = r"""#!/bin/sh
# A stand-in for sandbox-ai-ARCH.AppImage (tests/release_kit.py): boot.py, manifest.json and tree/ as in a real one.
set -e
self=$(readlink -f "$0")
line=$(awk '/^__APPDIR_TAR__$/ { print NR + 1; exit }' "$self")
if [ "$1" = "--appimage-extract" ]; then
  mkdir -p squashfs-root
  if [ -n "$2" ]; then tail -n +"$line" "$self" | tar -xz -C squashfs-root "./$2"
  else tail -n +"$line" "$self" | tar -xz -C squashfs-root; fi
  exit 0
fi
[ "$1" = "--appimage-extract-and-run" ] && shift
dir=$(mktemp -d "${TMPDIR:-/tmp}/appimage-stub.XXXXXX")
tail -n +"$line" "$self" | tar -xz -C "$dir"
status=0
APPIMAGE="$self" ARGV0="$0" APPDIR="$dir" python3 -I "$dir/usr/lib/sandbox-ai/boot.py" "$@" || status=$?
rm -rf "$dir"
exit $status
__APPDIR_TAR__
"""


def unpack_bundle(bundle, target):
    """The tree of a bundle .tar.gz at TARGET (without its top folder)."""
    target = Path(target)
    with tarfile.open(bundle, "r:gz") as tar:
        for member in tar.getmembers():
            relative = member.name.partition("/")[2]
            if not relative or not (member.isfile() or member.isdir()):
                continue
            path = target / relative
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(tar.extractfile(member).read())
            path.chmod(0o755 if member.mode & 0o111 else 0o644)
    return target


def fake_appimage(tree, bundle, out, arch="x86_64", work=None, pyside=True):
    """OUT/sandbox-ai-ARCH.AppImage for the release whose bundle is BUNDLE: the AppDir as scripts/build_appimage.sh
    lays it out (usr/lib/sandbox-ai/{boot.py, manifest.json, tree/sandbox-ai/...} with PACKAGING "appimage"), without
    a Python and PySide6 of its own (the command line runs with python3; the app, with PYSIDE - an empty pyside/
    folder -, with the PySide6 of the python3 on PATH)."""
    work = Path(work or Path(out) / f".appdir-{arch}")
    shutil.rmtree(work, ignore_errors=True)
    lib = work / "usr/lib/sandbox-ai"
    home = unpack_bundle(bundle, lib / "tree/sandbox-ai")
    (home / "PACKAGING").write_text("appimage\n")
    shutil.copy2(Path(tree) / "scripts/dist_boot.py", lib / "boot.py")
    done = subprocess.run([sys.executable, "-I", str(Path(tree) / "scripts/make_pyz.py"), "--manifest", "--tree",
                           str(lib / "tree"), "--kind", "appimage", "--home", "sandbox-ai", "--out",
                           str(lib / "manifest.json")], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    if pyside:
        (lib / "pyside").mkdir()
    target = Path(out) / assets.appimage(arch)
    tarred = subprocess.run(["tar", "-c", "-I", "gzip -1", "--sort=name", "--owner=0", "--group=0",
                             "--mtime=@1700000000", "-C", str(work), "."], capture_output=True, check=True).stdout
    target.write_bytes(APPIMAGE_STUB.encode() + tarred)
    target.chmod(0o755)
    shutil.rmtree(work, ignore_errors=True)
    return target


def assets_version(tree):
    return re.search(r'^__version__ = "([^"]+)"$', (Path(tree) / "sandbox/agent_sandbox.py").read_text(), re.M)[1]


def macos_app(bundle, target, python="python3"):
    """TARGET (…/sandbox-ai.app) laid out as scripts/build_macos_app.sh does it: Contents/Resources/sandbox-ai is the
    tree with PACKAGING "macos-app", Contents/MacOS/sandbox-ai and sandbox-ai-app the real launchers (packaging/
    macos-cli, macos-app). In place of the Python and the PySide6 a real one carries: Resources/python/bin/python3
    runs PYTHON, Resources/pyside is empty (the app then takes PYTHON's PySide6)."""
    target = Path(target)
    resources = target / "Contents/Resources"
    home = unpack_bundle(bundle, resources / "sandbox-ai")
    (home / "PACKAGING").write_text("macos-app\n")
    (target / "Contents/MacOS").mkdir(parents=True, exist_ok=True)
    for source, name in (("macos-cli", "sandbox-ai"), ("macos-app", "sandbox-ai-app")):
        shutil.copy2(REPOSITORY / "packaging" / source, target / "Contents/MacOS" / name)
        (target / "Contents/MacOS" / name).chmod(0o755)
    (resources / "python/bin").mkdir(parents=True)
    (resources / "python/bin/python3").write_text(f'#!/bin/sh\nexec {python} "$@"\n')
    (resources / "python/bin/python3").chmod(0o755)
    (resources / "pyside").mkdir()
    (target / "Contents/Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>CFBundleShortVersionString</key>'
        f"<string>{assets_version(home)}</string></dict></plist>\n")
    return target


def fake_dmg(bundle, out, arch="arm64", work=None, python="python3"):
    """OUT/sandbox-ai-ARCH.dmg for the tests: a tar archive of sandbox-ai.app and the Applications link, which the
    stand-in hdiutil (hdiutil_stub) "attaches" by unpacking it."""
    work = Path(work or Path(out) / f".dmg-{arch}")
    shutil.rmtree(work, ignore_errors=True)
    macos_app(bundle, work / "sandbox-ai.app", python)
    os.symlink("/Applications", work / "Applications")
    target = Path(out) / assets.dmg(arch)
    subprocess.run(["tar", "-c", "-I", "gzip -1", "--sort=name", "--owner=0", "--group=0", "--mtime=@1700000000",
                    "-f", str(target), "-C", str(work), "."], check=True)
    shutil.rmtree(work, ignore_errors=True)
    return target


def hdiutil_stub(folder):
    """A folder for PATH with stand-ins for macOS's hdiutil (attach: the tar archive unpacked at -mountpoint; detach:
    removed), ditto (cp -R) and codesign (says yes); each records its arguments in FOLDER/calls."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    scripts = {
        "hdiutil": 'echo "hdiutil $*" >> "$(dirname "$0")/calls"\n'
                   'case "$1" in\n'
                   '  attach) shift; while [ $# -gt 1 ]; do case "$1" in -mountpoint) mount=$2; shift 2 ;; '
                   '-*) shift ;; *) break ;; esac; done\n'
                   '    [ -n "$mount" ] || { mount=$2; }\n'
                   '    image=$1; mkdir -p "$mount" && tar -xz -f "$image" -C "$mount" ;;\n'
                   '  detach) shift; while [ $# -gt 1 ]; do shift; done\n'
                   '    chmod -R u+w "$1" 2>/dev/null; rm -rf "$1" ;;\n'
                   '  *) exit 64 ;;\nesac\n',
        "ditto": 'echo "ditto $*" >> "$(dirname "$0")/calls"\ncp -R "$1" "$2"\n',
        "codesign": 'echo "codesign $*" >> "$(dirname "$0")/calls"\nexit 0\n',
    }
    for name, body in scripts.items():
        path = folder / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)
    return folder


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def full_release(tree, out, key, work, python_package=True, videos=True):
    """Every file of a release of TREE in OUT, signed with KEY: make_dist.py's (bundle, .pyz, install.sh, the wheel
    and the sdist with PYTHON_PACKAGE), the stand-in AppImages and disk images of both architectures, a small
    sandbox-ai-videos.tar, SHA256SUMS and SHA256SUMS.sig."""
    out, work = Path(out), Path(work)
    version = assets_version(tree)
    make_dist(tree, out, *([] if python_package else ["--skip-python"]))
    bundle = out / assets.bundle(version)
    # One build per kind; the second architecture is the same file under its name (a hard link: the disk is small).
    first = fake_appimage(tree, bundle, out, assets.APPIMAGE_ARCHES[0])
    os.link(first, out / assets.appimage(assets.APPIMAGE_ARCHES[1]))
    first = fake_dmg(bundle, out, assets.MACOS_ARCHES[0])
    os.link(first, out / assets.dmg(assets.MACOS_ARCHES[1]))
    extra = []
    if videos:
        folder = work / f"videos-{version}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "sandbox-ai-app-01-start-de.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        (folder / "sandbox-ai-app-01-start-de.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nStart\n")
        extra = ["--videos", folder]
    sums_only(tree, out, *extra, "--sign", key.path)
    return out
