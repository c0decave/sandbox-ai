#!/usr/bin/env python3
"""Vendor xterm.js for the app's terminal tabs: no Node.js, standard library only.

  python3 scripts/vendor_xterm.py [--pins vendor/xterm.json] [--into sandbox_ai_app/web/vendor] [--require-pins]

For each package in the pin file it downloads the npm tarball of the pinned version from
registry.npmjs.org, checks it against the pinned integrity (a sha512 SRI value, as in package-lock.json)
and refuses it on a mismatch, then takes only the files it needs (regular files, never links, nothing
outside the package, bounded sizes) and writes them under fixed names into the target directory.

A package without a pinned integrity is checked against the registry's own dist.integrity instead, with a
warning (GitHub Actions ::warning:: format) that shows the value to pin; --require-pins refuses it.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
PINS = HERE.parent / "vendor" / "xterm.json"
TARGET = HERE.parent / "sandbox_ai_app" / "web" / "vendor"
REGISTRY = "https://registry.npmjs.org"
TARBALL_LIMIT = 20 << 20
FILE_LIMIT = 5 << 20
NAME = re.compile(r"@?[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)?")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?")
TARGET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


class Refused(Exception):
    pass


def fetch(url, limit=TARBALL_LIMIT):
    """The body of an https URL on the registry, at most LIMIT bytes."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != urllib.parse.urlsplit(REGISTRY).hostname:
        raise Refused(f"not a registry URL: {url}")
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream, application/json",
                                                   "User-Agent": "sandbox-ai-app vendor_xterm"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - https to the registry only
        if urllib.parse.urlsplit(response.geturl()).hostname != parsed.hostname:
            raise Refused(f"redirected away from the registry: {response.geturl()}")
        data = response.read(limit + 1)
    if len(data) > limit:
        raise Refused(f"{url} is larger than {limit >> 20} MiB")
    return data


def sri(data, algorithm="sha512"):
    return f"{algorithm}-" + base64.b64encode(hashlib.new(algorithm, data).digest()).decode()


def check_integrity(data, integrity):
    """True if DATA matches the SRI value INTEGRITY (sha512 only: weaker digests are refused)."""
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        raise Refused(f"only sha512 integrity values are accepted, not {integrity!r}")
    expected = integrity.split("-", 1)[1]
    try:
        expected_bytes = base64.b64decode(expected, validate=True)
    except ValueError:
        raise Refused(f"integrity is not valid base64: {integrity!r}") from None
    if len(expected_bytes) != 64:
        raise Refused(f"integrity is not a sha512 digest: {integrity!r}")
    return hashlib.sha512(data).digest() == expected_bytes


def tarball_url(name, version):
    return f"{REGISTRY}/{name}/-/{name.rsplit('/', 1)[-1]}-{version}.tgz"


def registry_dist(name, version, get=fetch):
    """(tarball URL, integrity) the registry publishes for NAME@VERSION."""
    url = f"{REGISTRY}/{urllib.parse.quote(name, safe='@')}/{version}"
    body = get(url, 2 << 20)
    try:
        document = json.loads(body)
    except ValueError:
        # A proxy, captive portal or error page answered instead of the registry: say what came back.
        start = body[:120].decode("utf-8", "replace").encode("unicode_escape").decode()
        raise Refused(f"{url} did not answer with JSON (first bytes: {start!r}); a proxy or captive portal in the "
                      "way? Pin the integrity in vendor/xterm.json to need no metadata") from None
    if not isinstance(document, dict):
        raise Refused(f"{url} answered with JSON that is not package metadata")
    dist = document.get("dist") or {}
    if document.get("name") != name or document.get("version") != version:
        raise Refused(f"the registry answered for {document.get('name')}@{document.get('version')}")
    return dist.get("tarball"), dist.get("integrity")


def extract(data, files):
    """{target name: bytes} of FILES ({path in the package: target name}) from the tarball DATA. Only regular
    files are taken, each exactly once; links, devices and anything else under a wanted name are refused."""
    wanted = {f"package/{path}": target for path, target in files.items()}
    found = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            if name not in wanted:
                continue
            if not member.isreg():
                raise Refused(f"{name} in the tarball is not a regular file")
            if name in found:
                raise Refused(f"{name} is in the tarball more than once")
            if member.size > FILE_LIMIT:
                raise Refused(f"{name} is larger than {FILE_LIMIT >> 20} MiB")
            stream = archive.extractfile(member)
            content = stream.read(FILE_LIMIT + 1) if stream else b""
            if len(content) != member.size:
                raise Refused(f"{name} is damaged")
            found[name] = content
    missing = sorted(set(wanted) - set(found))
    if missing:
        raise Refused(f"not in the tarball: {', '.join(missing)}")
    return {wanted[name]: content for name, content in found.items()}


def write_files(target, contents):
    """Write CONTENTS ({name: bytes}) into TARGET under their own fixed names, each atomically."""
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise Refused(f"{target} is a symbolic link")
    for name, content in contents.items():
        if not TARGET_NAME.fullmatch(name) or name in (".", ".."):
            raise Refused(f"invalid target file name {name!r}")
        fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=target)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            os.chmod(temporary, 0o644)
            os.replace(temporary, target / name)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


def warn(message, out=print):
    out(f"::warning title=xterm.js not pinned::{message}")


def vendor(pins_path=PINS, target=TARGET, require_pins=False, get=fetch, out=print):
    pins = json.loads(Path(pins_path).read_text())
    manifest = {}
    contents = {}
    for name, pin in sorted(pins.get("packages", {}).items()):
        version, integrity, files = pin.get("version", ""), pin.get("integrity", ""), pin.get("files", {})
        if not NAME.fullmatch(name) or not VERSION.fullmatch(version):
            raise Refused(f"invalid package or version in {pins_path}: {name}@{version}")
        if not files or not all(TARGET_NAME.fullmatch(t) for t in files.values()):
            raise Refused(f"{name}: list the files to take, each with a plain target name")
        url = tarball_url(name, version)
        if not integrity:
            if require_pins:
                raise Refused(f"{name}@{version} has no pinned integrity in {pins_path}")
            registry_url, integrity = registry_dist(name, version, get)
            if registry_url != url:
                raise Refused(f"the registry names an unexpected tarball for {name}@{version}: {registry_url}")
            if not integrity:
                raise Refused(f"the registry gives no integrity for {name}@{version}")
            warn(f"{name}@{version} is not pinned; verified against the registry's own dist.integrity only. "
                 f"Check it and pin it in {Path(pins_path).name}: \"integrity\": \"{integrity}\"", out)
        data = get(url)
        if not check_integrity(data, integrity):
            raise Refused(f"{name}@{version}: the tarball does not match the integrity {integrity}; nothing written "
                          f"(it is {sri(data)})")
        taken = extract(data, files)
        overlap = set(taken) & set(contents)
        if overlap:
            raise Refused(f"two packages write {', '.join(sorted(overlap))}")
        contents.update(taken)
        manifest[name] = {"version": version, "integrity": integrity, "pinned": bool(pin.get("integrity")),
                          "files": {t: hashlib.sha256(c).hexdigest() for t, c in sorted(taken.items())}}
        out(f"{name}@{version}: verified, {len(taken)} file(s)")
    contents["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    write_files(target, contents)
    out(f"written to {target}")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pins", type=Path, default=PINS)
    parser.add_argument("--into", type=Path, default=TARGET)
    parser.add_argument("--require-pins", action="store_true", help="refuse packages without a pinned integrity")
    args = parser.parse_args(argv)
    try:
        vendor(args.pins, args.into, args.require_pins)
    except (Refused, OSError, ValueError, tarfile.TarError) as exc:
        print(f"vendor_xterm: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
