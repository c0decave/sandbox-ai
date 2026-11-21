"""The files of a sandbox-ai release, in one place: their names, who builds them, and which one each kind of
installation downloads to update itself.

  sandbox-ai-bundle-X.Y.Z.tar.gz         install.sh's installations (a user's own, a system one): the whole tree
  sandbox_ai_app-X.Y.Z-py3-none-any.whl  pip and pipx installations (the same file PyPI serves)
  sandbox_ai_app-X.Y.Z.tar.gz            its source package
  install.sh                             first installs (it carries the release keys)
  sandbox-ai.pyz                         the command line in one file
  sandbox-ai-x86_64.AppImage, sandbox-ai-aarch64.AppImage   the app for Linux
  sandbox-ai-arm64.dmg, sandbox-ai-x86_64.dmg               sandbox-ai.app for macOS
  sandbox-ai-videos.tar                  the videos about the app's pages (the app downloads them once)
  SHA256SUMS, SHA256SUMS.sig             the checksums of every file above, and their signature by a release key

scripts/make_dist.py, scripts/publish_release.py, scripts/release_check.py, scripts/release_notes.py and the updaters
(updates.py) take the names from here; install.sh carries its own copy of the bundle's name (it runs before any tree
is installed; tests/test_release_assets.py keeps the two in step). Standard library only.
"""
import platform
import re

SUMS, SIGNATURE = "SHA256SUMS", "SHA256SUMS.sig"
INSTALLER, PYZ, VIDEOS = "install.sh", "sandbox-ai.pyz", "sandbox-ai-videos.tar"
PYPI_NAME = "sandbox-ai-app"                      # the PyPI project; its files start with sandbox_ai_app-
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:[.+-][0-9A-Za-z.+-]+)?"   # install.sh's and updates.py's
BUNDLE = re.compile(r"sandbox-ai-bundle-(" + VERSION + r")\.tar\.gz")  # install.sh's ASSET
APPIMAGE_ARCHES = ("x86_64", "aarch64")           # release.yml: ubuntu-24.04, ubuntu-24.04-arm
MACOS_ARCHES = ("arm64", "x86_64")                # release.yml: macos-15, macos-15-intel
# platform.machine() of the machines the packages are built for, as the files' names say it
MACHINES = {"appimage": {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"},
            "macos-app": {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x86_64", "amd64": "x86_64"}}
# Who makes a file: "ci" - release.yml builds and checks it; "maintainer" - made where the release is signed
# (the videos are rendered there: make_dist.py --videos DIR; the signature needs the release key, which never
# comes near CI).
CI, MAINTAINER = "ci", "maintainer"
KINDS = ("user", "system", "pip", "pyz", "appimage", "macos-app")  # the installations that update from a release


def bundle(version):
    return f"sandbox-ai-bundle-{version}.tar.gz"


def wheel(version):
    return f"{PYPI_NAME.replace('-', '_')}-{version}-py3-none-any.whl"


def sdist(version):
    return f"{PYPI_NAME.replace('-', '_')}-{version}.tar.gz"


def appimage(arch):
    return f"sandbox-ai-{arch}.AppImage"


def dmg(arch):
    return f"sandbox-ai-{arch}.dmg"


def artifacts(version):
    """Every file of the release VERSION but SHA256SUMS and its signature: [{"name", "kind", "by", "for"}], in the
    order of the release page's table. KIND: installer, appimage, dmg, pyz, wheel, sdist, bundle, videos."""
    return [
        {"name": INSTALLER, "kind": "installer", "by": CI,
         "for": "Linux and macOS: installs the command line and the desktop app, kept up to date (recommended)"},
        {"name": appimage("x86_64"), "kind": "appimage", "by": CI, "arch": "x86_64",
         "for": "Linux on x86_64: the desktop app with the command line inside, nothing installed"},
        {"name": appimage("aarch64"), "kind": "appimage", "by": CI, "arch": "aarch64",
         "for": "Linux on ARM (aarch64): the same"},
        {"name": dmg("arm64"), "kind": "dmg", "by": CI, "arch": "arm64",
         "for": "macOS on Apple silicon (macOS 14+): sandbox-ai.app"},
        {"name": dmg("x86_64"), "kind": "dmg", "by": CI, "arch": "x86_64",
         "for": "macOS on Intel (macOS 14+): sandbox-ai.app"},
        {"name": PYZ, "kind": "pyz", "by": CI,
         "for": "Linux and macOS: the command line in one file, any Python 3.9+"},
        {"name": wheel(version), "kind": "wheel", "by": CI,
         "for": "the package that PyPI serves (`pipx install sandbox-ai-app`): the command line and the web "
                "interface, the desktop app with the extra [gui]; pip and pipx installations update with it"},
        {"name": sdist(version), "kind": "sdist", "by": CI, "for": "its source package"},
        {"name": bundle(version), "kind": "bundle", "by": CI,
         "for": "the whole tree, reproducible: what install.sh installs and updates with"},
        {"name": VIDEOS, "kind": "videos", "by": MAINTAINER,
         "for": "the videos about the app's pages (the app downloads them once)"},
    ]


def required(version, by=None, signed=True):
    """The names a complete release of VERSION has: every artifact (BY "ci": only those release.yml builds), then
    SHA256SUMS and - SIGNED - SHA256SUMS.sig."""
    names = [entry["name"] for entry in artifacts(version) if by is None or entry["by"] == by]
    return names + [SUMS] + ([SIGNATURE] if signed else [])


def missing(names, version, by=None, signed=True):
    """The required files (required) that NAMES lacks, in the release page's order."""
    have = set(names)
    return [name for name in required(version, by, signed) if name not in have]


def unknown(names, version):
    """The NAMES that are no file of the release VERSION (another version's bundle, a stray file)."""
    known = set(required(version))
    return sorted(name for name in names if name not in known)


def arch_of(kind, machine=None):
    """The architecture in the name of KIND's file ("appimage", "macos-app") for this machine (MACHINE:
    platform.machine()), or None when no package is built for it."""
    machine = (platform.machine() if machine is None else machine).lower()
    return MACHINES.get(kind, {}).get(machine)


def asset_for(kind, version, machine=None):
    """The release file an installation of KIND (KINDS) downloads to become VERSION, or None when there is none for
    this machine: install.sh's installations the bundle, pip the wheel, the packages themselves."""
    if kind in ("user", "system"):
        return bundle(version)
    if kind == "pip":
        return wheel(version)
    if kind == "pyz":
        return PYZ
    arch = arch_of(kind, machine)
    if arch is None:
        return None
    return appimage(arch) if kind == "appimage" else dmg(arch) if kind == "macos-app" else None


def checksums(text):
    """{name: sha256} of a SHA256SUMS text (sha256sum's lines; "*name" for binary mode), as install.sh reads it."""
    found = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            found[parts[1].lstrip("*")] = parts[0]
    return found


def bundle_version(names):
    """The version of the release whose files NAMES are (the names a SHA256SUMS lists): the one of its bundle; None
    without a bundle, or with bundles of two versions (then it is no release's list)."""
    versions = {match[1] for name in names if isinstance(name, str) and (match := BUNDLE.fullmatch(name))}
    return versions.pop() if len(versions) == 1 else None
