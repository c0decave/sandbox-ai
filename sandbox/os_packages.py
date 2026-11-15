"""The operating system packages of an image: which distribution it is (its /etc/os-release), how that
distribution's package manager installs (apt-get, apk, dnf, pacman, zypper), and its package index for a search
(sandbox-ai images search). Standard library only; no Podman here - images.py runs the container.

The agent images and their variants are Debian 12: their extensions install with apt-get. Your own images (images
pull|build-image) may be any distribution: `sandbox-ai images derive` adds packages to them with their own package
manager. Every manager named here checks its packages' signatures against the keys the image ships, as apt does."""
import json
import os
from pathlib import Path
import re
import shlex
import time

# family -> what sandbox-ai knows of it. PATTERN: a package as it may be given (a name, a pin where the manager has
# one); every one starts with a letter or digit (never an option).
FAMILIES = {
    "debian": {"label": "Debian/Ubuntu", "manager": "apt-get",
               "pattern": r"[a-z0-9][a-z0-9+.-]{0,99}(=[A-Za-z0-9.+~:-]{1,64})?",
               "help": "a Debian/Ubuntu package name, optionally =VERSION", "pin": "="},
    "alpine": {"label": "Alpine", "manager": "apk",
               "pattern": r"[a-z0-9][a-z0-9+._-]{0,99}(=[A-Za-z0-9.+_~-]{1,64})?",
               "help": "an Alpine package name, optionally =VERSION (e.g. curl=8.11.1-r0)", "pin": "="},
    "fedora": {"label": "Fedora/RHEL", "manager": "dnf",
               "pattern": r"[A-Za-z0-9][A-Za-z0-9+._-]{0,127}",
               "help": "a Fedora/RHEL package name, optionally -VERSION (e.g. jq or jq-1.7.1)", "pin": "-"},
    "arch": {"label": "Arch Linux", "manager": "pacman",
             "pattern": r"[a-z0-9][a-z0-9@._+-]{0,99}",
             "help": "an Arch Linux package name (pacman installs the repository's current version: no pins)",
             "pin": None},
    "suse": {"label": "openSUSE/SLES", "manager": "zypper",
             "pattern": r"[A-Za-z0-9][A-Za-z0-9+._-]{0,99}(=[A-Za-z0-9.+~_:-]{1,64})?",
             "help": "an openSUSE/SLES package name, optionally =VERSION", "pin": "="},
}
# os-release IDs (and ID_LIKE words) of each family.
IDS = {"debian": ("debian", "ubuntu", "raspbian", "linuxmint", "pop", "kali", "devuan"),
       "alpine": ("alpine", "postmarketos", "wolfi", "chainguard"),
       "fedora": ("fedora", "rhel", "centos", "rocky", "almalinux", "ol", "amzn", "fedora-asahi-remix"),
       "arch": ("arch", "archarm", "manjaro", "endeavouros"),
       "suse": ("opensuse", "opensuse-leap", "opensuse-tumbleweed", "sles", "sled", "suse")}
DEBIAN_12 = {"ID": "debian", "VERSION_ID": "12", "VERSION_CODENAME": "bookworm",
             "PRETTY_NAME": "Debian GNU/Linux 12 (bookworm)"}
INDEX_AGE = 7 * 86400  # a cached index older than this is fetched anew when it can be
RESULT_LIMIT = 200
OUTPUT_LIMIT = 96 << 20  # what an index listing may print


def family_of(release):
    """The family (a key of FAMILIES) of an os-release {ID, ID_LIKE, ...}, else None."""
    release = release if isinstance(release, dict) else {}
    words = [str(release.get("ID") or "").lower()] + str(release.get("ID_LIKE") or "").lower().split()
    for word in words:
        for family, ids in IDS.items():
            if word in ids or (family == "suse" and word.startswith("opensuse")):
                return family
    return None


def describe(release):
    """{family, label, manager, system} of an os-release."""
    family = family_of(release)
    system = (release or {}).get("PRETTY_NAME") or " ".join(
        str((release or {}).get(key) or "") for key in ("ID", "VERSION_ID")).strip() or "unknown"
    return {"family": family, "label": FAMILIES[family]["label"] if family else None,
            "manager": FAMILIES[family]["manager"] if family else None, "system": system}


def check_name(family, value):
    """None if VALUE is a package FAMILY's manager may be given, else the reason."""
    if family not in FAMILIES:
        return f"unknown distribution {family}"
    if not isinstance(value, str) or not re.fullmatch(FAMILIES[family]["pattern"], value):
        return f"{value!r} is not {FAMILIES[family]['help']}"
    return None


def package_name(family, value):
    """VALUE without its pin (where the family's managers pin with =)."""
    return value.split("=", 1)[0] if FAMILIES.get(family, {}).get("pin") == "=" else value


def install_lines(family, packages):
    """The Containerfile lines that install PACKAGES with FAMILY's manager (non-interactively, without the cache
    left behind). Debian's are exactly those of the extensions before (their hash does not change)."""
    q = shlex.quote
    names = " ".join(q(p) for p in packages)
    if family == "debian":
        return ["RUN apt-get update && apt-get install -y --no-install-recommends \\", f"    {names} \\",
                "    && rm -rf /var/lib/apt/lists/*"]
    if family == "alpine":
        return [f"RUN apk add --no-cache {names}"]
    if family == "fedora":  # dnf (Fedora, RHEL, Rocky, Alma), else microdnf (the minimal images)
        return ["RUN if command -v dnf >/dev/null 2>&1; then \\",
                f"      dnf install -y --setopt=install_weak_deps=False {names} && dnf clean all; \\",
                f"    else microdnf install -y {names} && microdnf clean all; fi"]
    if family == "arch":  # Arch upgrades as a whole: a partial upgrade (-Sy PKG) breaks it
        return [f"RUN pacman -Syu --noconfirm --needed {names} && rm -rf /var/cache/pacman/pkg/*"]
    if family == "suse":
        return [f"RUN zypper -n refresh && zypper -n install --no-recommends {names} && zypper -n clean --all"]
    raise ValueError(f"unknown distribution {family}")


# What a throwaway container of the image prints for its package index: noise on standard error, then a line
# "@@index" and the index. Exit status 3: the index could not be fetched (no network); 4: no tool that lists it.
INDEX_SCRIPTS = {
    "debian": (
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get -q -o APT::Sandbox::User=root -o APT::Update::Error-Mode=any update >&2 || exit 3; "
        "echo @@index; "
        "apt-cache dumpavail | awk '/^Package: /{p=$2} /^Version: /{v=$2} "
        "/^Description: /{printf \"%s\\t%s\\t%s\\n\", p, v, substr($0, 14)}'"),
    "alpine": (
        "apk update -q >&2 || exit 3; echo @@index; "
        "for f in /var/cache/apk/APKINDEX.*.tar.gz; do [ -f \"$f\" ] && tar -xzOf \"$f\" APKINDEX; echo; done | "
        "awk '/^P:/{p=substr($0,3)} /^V:/{v=substr($0,3)} /^T:/{t=substr($0,3)} "
        "/^$/{if (p != \"\") printf \"%s\\t%s\\t%s\\n\", p, v, t; p=\"\"; v=\"\"; t=\"\"}'"),
    "fedora": (
        "command -v dnf >/dev/null 2>&1 || exit 4; dnf -q makecache >&2 || exit 3; echo @@index; "
        "dnf -q repoquery --available --qf '%{name}\\t%{evr}\\t%{summary}\\n'"),
    "arch": "pacman -Sy --noconfirm >&2 || exit 3; echo @@index; pacman -Ss",
    "suse": "zypper -n -q refresh >&2 || exit 3; echo @@index; zypper -n -q --no-refresh search -t package",
}
NO_LISTING = {"fedora": "this image has no dnf (only microdnf, which cannot list its repositories): type the "
                        "package's exact name"}


def parse_index(family, text):
    """[(name, version, summary)] of what INDEX_SCRIPTS[FAMILY] printed (each name once)."""
    _, marker, body = text.partition("@@index\n")
    lines = (body if marker else text).splitlines()
    found = {}
    if family == "arch":  # "repo/name version [installed]" then "    description"
        name = version = None
        for line in lines:
            if line[:1] not in (" ", "\t", "") and "/" in line.split(" ", 1)[0]:
                head = line.split()
                name, version = head[0].split("/", 1)[1], head[1] if len(head) > 1 else ""
                found[name] = (name, version, "")
            elif name and line.strip():
                found[name] = (name, version, line.strip())
                name = None
    elif family == "suse":  # S | Name | Summary | Type
        for line in lines:
            cells = [cell.strip() for cell in line.split("|")]
            if len(cells) >= 4 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._-]*", cells[1]) and cells[1] != "Name" \
                    and cells[3] in ("package", ""):
                found[cells[1]] = (cells[1], "", cells[2])
    else:
        for line in lines:
            parts = line.replace("\\t", "\t").split("\t")
            if len(parts) >= 2 and re.fullmatch(r"[A-Za-z0-9@+._-]{1,128}", parts[0]):
                found[parts[0]] = (parts[0], parts[1].strip(), "\t".join(parts[2:]).strip())
    return sorted(found.values())


def search(rows, query, limit=50):
    """The ROWS (name, version, summary) that match every word of QUERY in their name or summary: the exact name
    first, then names that begin with it, names that hold it, summaries that hold it."""
    words = str(query or "").lower().split()
    if not words:
        return list(rows[:limit])
    first, scored = words[0], []
    for row in rows:
        name, summary = row[0].lower(), row[2].lower()
        if not all(word in name or word in summary for word in words):
            continue
        rank = 0 if name == first else 1 if name.startswith(first) else 2 if first in name else 3
        scored.append((rank, len(name), name, row))
    scored.sort(key=lambda item: item[:3])
    return [item[3] for item in scored[:limit]]


def cache_dir():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "sandbox-ai" / "package-index"


def cache_path(family, image_id):
    key = re.sub(r"[^a-f0-9]", "", str(image_id).removeprefix("sha256:").lower())[:32] or "unknown"
    return cache_dir() / f"{family}-{key}.json"


def load_index(family, image_id):
    """The cached index of the image IMAGE_ID ({time, rows, ...}), else None."""
    path = cache_path(family, image_id)
    try:
        if path.is_symlink():
            return None
        document = json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(document, dict) or not isinstance(document.get("rows"), list) or \
            document.get("family") != family:
        return None
    document["rows"] = [tuple(str(v) for v in row[:3]) for row in document["rows"]
                        if isinstance(row, list) and len(row) >= 3]
    return document


def save_index(family, image_id, image, rows):
    """The index of IMAGE (its ID) cached, privately; the document saved."""
    document = {"version": 1, "family": family, "id": str(image_id), "image": image, "time": int(time.time()),
                "rows": [list(row) for row in rows]}
    path = cache_path(family, image_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(document, handle)
    os.replace(temporary, path)
    document["rows"] = list(rows)
    return document
