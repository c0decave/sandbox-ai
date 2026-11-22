#!/usr/bin/env python3
"""The pins of the image recipe android-dev (sandbox/recipes/android-dev): sdk.lock and the package.xml of every
Android SDK package in it, made from a copy of Google's SDK repository as NixOS keeps it (nixpkgs
pkgs/development/mobile/androidenv/repo.json, generated from dl.google.com/android/repository/*.xml).

  python3 scripts/android_dev_lock.py --repo-json repo.json --source nixpkgs@COMMIT   write sdk.lock and package-xml/
  python3 scripts/android_dev_lock.py --repo-json repo.json --check                    exit 1 when they differ

Google publishes the SHA-1 and the size of each SDK archive (in its repository XML, the NDK also on its GitHub
release page) and no SHA-256; the lock pins both, and the SHA-256 of a Google archive once a build has confirmed it
(install.sh prints it; a SHA-256 already in sdk.lock is kept here). Gradle and Kotlin are GitHub release files with
the SHA-256 their publishers give. Which version: the newest release of each package that was at least 14 days old
(sandbox-ai's rule for third-party releases) and under Google's android-sdk-license (the preview channels carry the
preview license) - change PACKAGES below and run this again.

The package.xml is what sdkmanager writes beside a package it installs (the format nixpkgs' androidenv writes for
the same purpose, deploy-androidpackages.nix): without it the SDK tools and the Android Gradle plugin do not see a
package that was unpacked by hand. Standard library only.
"""
import argparse
import json
from pathlib import Path
import re
import sys
from xml.sax.saxutils import escape, quoteattr

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "sandbox" / "recipes" / "android-dev"
SDK = "/opt/android-sdk"

# (section of repo.json, key, why): the API levels 37 (the current stable one), 36 and 35 with the build-tools of
# each, one NDK, the CMake of the Android Gradle plugin's default (3.22.1) and the newest, the emulator of the stable
# channel and one x86_64 system image of API 37.
# The third field: by when it was in Google's repository (the first nixpkgs snapshot that had it: 9e22abf3f7d8 of
# 2026-08-11, d206ccbcf9b8 of 2026-10-09, 89077a88f318 of 2026-11-01; the NDK r30's release page says 2026-09-08).
PACKAGES = [
    ("packages/cmdline-tools", "23.0", "2026-10-09"),
    ("packages/platform-tools", "37.0.1", "2026-08-11"),
    ("packages/platforms", "37.0", "2026-08-11"),
    ("packages/platforms", "36", "2026-08-11"),
    ("packages/platforms", "35", "2026-08-11"),
    ("packages/build-tools", "37.0.0", "2026-08-11"),
    ("packages/build-tools", "36.0.0", "2026-08-11"),
    ("packages/build-tools", "35.0.0", "2026-08-11"),
    ("packages/ndk", "30.0.16248370", "2026-09-08"),
    ("packages/cmake", "4.1.2", "2026-08-11"),
    ("packages/cmake", "3.22.1", "2026-08-11"),
    ("packages/emulator", "37.2.13", "2026-11-01"),
    ("images", "37.0/google_apis/x86_64", "2026-11-01"),
]
# The files that are not Google's, each with the SHA-256 its publisher gives (checked when this list was made).
OTHERS = [
    # Gradle 9.8.2 (Gradle's GitHub distribution, the same file as services.gradle.org's); the SHA-256 is the release
    # asset's digest, recomputed from the download.
    ("Gradle 9.8.2, released 2026-10-19; the SHA-256 of its GitHub release asset",
     ("-", "9.8.2", "/opt/gradle", 151620138, "-",
      "21bd5da2de49b642904077f38c0efdc7778e92b3ed0889392dca61b97e389607",
      "https://github.com/gradle/gradle-distributions/releases/download/v9.8.2/gradle-9.8.2-bin.zip")),
    # The Kotlin compiler 2.4.21; the SHA-256 is JetBrains' kotlin-compiler-2.4.21.zip.sha256.
    ("the Kotlin compiler 2.4.21, released 2026-10-08; the SHA-256 of JetBrains' kotlin-compiler-2.4.21.zip.sha256",
     ("-", "2.4.21", "/opt/kotlinc", 89733618, "-",
      "7cc140e76daf416a0424a5557f99afd7ca89ebd463ea1101261e3e01c33e75cd",
      "https://github.com/JetBrains/kotlin/releases/download/v2.4.21/kotlin-compiler-2.4.21.zip")),
]
NAMESPACES = (
    'xmlns:ns2="http://schemas.android.com/repository/android/common/02" '
    'xmlns:ns3="http://schemas.android.com/repository/android/common/01" '
    'xmlns:ns4="http://schemas.android.com/repository/android/generic/01" '
    'xmlns:ns5="http://schemas.android.com/repository/android/generic/02" '
    'xmlns:ns6="http://schemas.android.com/sdk/android/repo/addon2/01" '
    'xmlns:ns7="http://schemas.android.com/sdk/android/repo/addon2/02" '
    'xmlns:ns8="http://schemas.android.com/sdk/android/repo/addon2/03" '
    'xmlns:ns9="http://schemas.android.com/sdk/android/repo/repository2/01" '
    'xmlns:ns10="http://schemas.android.com/sdk/android/repo/repository2/02" '
    'xmlns:ns11="http://schemas.android.com/sdk/android/repo/repository2/03" '
    'xmlns:ns12="http://schemas.android.com/sdk/android/repo/sys-img2/03" '
    'xmlns:ns13="http://schemas.android.com/sdk/android/repo/sys-img2/02" '
    'xmlns:ns14="http://schemas.android.com/sdk/android/repo/sys-img2/01" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"')
HEADER = """# The downloads of the image recipe android-dev, each pinned by its size and checksum; install.sh fetches them while
# the image is built (never at run time), stops the build on any difference and unpacks them in place.
# Made by scripts/android_dev_lock.py from Google's SDK repository as nixpkgs keeps it ({source}).
#
# sha1: Google's own checksum of an SDK archive (its repository XML; the NDK's also on github.com/android/ndk).
# sha256: Google publishes none - "-" until a build has confirmed it: install.sh then prints the SHA-256 it computed
# ("unconfirmed SHA-256"); write it here and every later build checks it too. Gradle's and Kotlin's are their
# publishers' SHA-256.
# Every archive: by downloading and building it you accept the Android Software Development Kit License Agreement
# (https://developer.android.com/studio/terms), the license all of them are published under.
#
# package                                     version        dest                                                     size        sha1                                      sha256                                                            url
"""


def ordered(details):
    """The child elements of a repo.json element ("name:INDEX" keys) in their original order; a name seen again is
    left out (repo.json merges the elements of several revisions of a system image: its second tag, vendor and abi)."""
    keys = [k for k in details if k != "element-attributes"]
    keys.sort(key=lambda k: int(k.split(":", 1)[1]) if ":" in k and k.split(":", 1)[1].isdigit() else 0)
    seen, result = set(), []
    for key in keys:
        name = key.split(":", 1)[0]
        if name in seen:
            continue
        seen.add(name)
        result.append((name, details[key]))
    return result


def element(name, value):
    if isinstance(value, str):
        return f"<{name}>{escape(value)}</{name}>"
    attributes = "".join(f" {k}={quoteattr(v)}" for k, v in (value.get("element-attributes") or {}).items())
    children = "".join(element(child, inner) for child, inner in ordered(value))
    return f"<{name}{attributes}>{children}</{name}>" if children else f"<{name}{attributes}/>"


def package_xml(entry, licenses):
    """The package.xml sdkmanager would write for ENTRY (a repo.json package)."""
    license_id = entry["license"]
    text = "---".join(licenses[license_id])
    parts = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             f"<ns2:repository {NAMESPACES}>",
             f'<license id={quoteattr(license_id)} type="text">{escape(text)}</license>',
             f'<localPackage path={quoteattr(entry["path"].replace("/", ";"))} obsolete="false">',
             element("type-details", entry["type-details"]),
             element("revision", entry["revision-details"])]
    if entry.get("dependencies"):
        parts.append(element("dependencies", entry["dependencies"]))
    parts += [f"<display-name>{escape(entry['displayName'])}</display-name>",
              f"<uses-license ref={quoteattr(license_id)}/>", "</localPackage>", "</ns2:repository>"]
    return "\n".join(parts) + "\n"


def lookup(repo, section, key):
    node = repo
    for part in section.split("/") + key.split("/"):
        node = node[part]
    return node


def linux_archive(entry):
    found = [a for a in entry["archives"] if a.get("os") in ("linux", "all") and a.get("arch") in ("all", "x64", None)]
    if len(found) != 1:
        raise SystemExit(f"{entry['path']}: {len(found)} archives for Linux x86_64")
    return found[0]


def xml_name(package):
    return package.replace(";", "_") + ".xml"


def confirmed(lock_text):
    """{url: sha256} of the rows of an existing sdk.lock that have one."""
    found = {}
    for line in lock_text.splitlines():
        words = line.split()
        if len(words) == 7 and not line.startswith("#") and re.fullmatch(r"[0-9a-f]{64}", words[5]):
            found[words[6]] = words[5]
    return found


def generate(repo, source, old_lock=""):
    """({file name: text}): sdk.lock and package-xml/NAME.xml."""
    kept = confirmed(old_lock)
    rows, files = [], {}
    for section, key, by in PACKAGES:
        entry = lookup(repo, section, key)
        if entry["license"] != "android-sdk-license":
            raise SystemExit(f"{entry['path']}: {entry['license']} (a preview, not the stable channel)")
        archive = linux_archive(entry)
        package = entry["path"].replace("/", ";")
        version = ".".join(v for _, v in ordered(entry["revision-details"]))
        if section == "packages/platforms" or section == "images":
            version = f"r{int(version):02d}"
        rows.append((f"{entry['displayName']} {version}, in Google's repository by {by}",
                     (package, version, f"{SDK}/{entry['path']}", archive["size"], archive["sha1"],
                      kept.get(archive["url"], "-"), archive["url"])))
        files[f"package-xml/{xml_name(package)}"] = package_xml(entry, repo["licenses"])
    rows += OTHERS
    lines = [HEADER.format(source=source)]
    for comment, (package, version, dest, size, sha1, sha256, url) in rows:
        lines.append(f"# {comment}\n")
        lines.append(f"{package:<45} {version:<14} {dest:<56} {size:<11} {sha1:<41} {sha256:<65} {url}\n")
    files["sdk.lock"] = "".join(lines)
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo-json", required=True, help="nixpkgs' pkgs/development/mobile/androidenv/repo.json")
    parser.add_argument("--source", default=None, help="where it is from, e.g. nixpkgs@f3a0ff26c758 (kept otherwise)")
    parser.add_argument("--check", action="store_true", help="change nothing; exit 1 when the files differ")
    args = parser.parse_args(argv)
    repo = json.loads(Path(args.repo_json).read_text(encoding="utf-8"))
    lock = TARGET / "sdk.lock"
    old = lock.read_text(encoding="utf-8") if lock.exists() else ""
    source = args.source
    if source is None:
        match = re.search(r"as nixpkgs keeps it \(([^)]*)\)", old)
        source = match.group(1) if match else "nixpkgs"
    files = generate(repo, source, old)
    differ = [name for name, text in files.items()
              if not (TARGET / name).exists() or (TARGET / name).read_text(encoding="utf-8") != text]
    stale = sorted(p.name for p in (TARGET / "package-xml").glob("*.xml")
                   if f"package-xml/{p.name}" not in files) if (TARGET / "package-xml").is_dir() else []
    if args.check:
        for name in differ + [f"package-xml/{name} (not in the lock)" for name in stale]:
            print(f"differs: {name}")
        return 1 if differ or stale else 0
    for name, text in files.items():
        (TARGET / name).parent.mkdir(parents=True, exist_ok=True)
        (TARGET / name).write_text(text, encoding="utf-8")
    for name in stale:
        (TARGET / "package-xml" / name).unlink()
    print(f"wrote {len(files)} files in {TARGET}" + (f", removed {len(stale)}" if stale else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
