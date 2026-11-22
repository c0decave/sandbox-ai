#!/usr/bin/env python3
"""sandbox-ai toolchain: build tools (Go, Rust, a JDK, Node, Python ...) mounted read-only into a sandbox.

  sandbox-ai toolchain catalog [--json] [--catalog FILE_OR_URL]
                                              what the signed catalog offers for this machine
  sandbox-ai toolchain add NAME[@VERSION] [--unsigned] [--catalog FILE_OR_URL]
                                              download it, check it, unpack it into the store
  sandbox-ai toolchain list [--run RUN] [--json]
                                              the store (with the running sandboxes that use each); --run: a
                                              running sandbox's toolchains
  sandbox-ai toolchain show NAME[@VERSION] [--json]
                                              its manifest: commands, variables, hosts, what it needs, its sources
  sandbox-ai toolchain remove NAME[@VERSION]  out of the store (refused while a running sandbox uses it)
  sandbox-ai toolchain attach [RUN] NAME[@VERSION] [--offline]
                                              mount it into the running sandbox, its commands on PATH
  sandbox-ai toolchain detach [RUN] NAME [--force]
                                              take it out again

At a start: sandbox-ai AGENT --toolchain NAME[@VERSION] (repeatable; in .sandbox-ai.json among its "options").

A toolchain is one archive of the files of one prefix and its manifest, an entry of the signed catalog
(toolchains.json of sandbox-ai-mcp, signed as its MCP catalog is: sandbox/mcp-signers). `add` takes an entry only
from a catalog whose signature verifies (--unsigned: one of an unsigned catalog of your own, said at every start;
never one whose signature is bad), checks the archive against the entry's SHA-256 before anything of it is unpacked
and unpacks it without trusting it (mcp_rootfs.unpack) into ~/.local/share/sandbox-ai/toolchains/SHA256/, read-only.

In the sandbox it is mounted read-only (nosuid, nodev) at /opt/toolchains/NAME. /opt/toolchains itself is a folder
of the run's session directory on the host, read-only in the sandbox: sandbox-ai writes one small wrapper per
command into its bin/ (first on the agent's PATH) and the toolchains' variables into env.sh. RUN as for
`sandbox-ai net`. `attach` runs nothing in the sandbox: a helper of your own user enters the sandbox's user
namespace, clones the store's tree (open_tree), makes the clone read-only (mount_setattr; Linux 5.12) and moves it
onto the mount point in each of the sandbox's own mount namespaces (move_mount), the agent's under Firejail
included. The registries a toolchain names are opened as `sandbox-ai net allow` opens hosts (the run's security
mode decides: high and paranoid keep them closed, the toolchain is mounted all the same); --offline opens none.
`detach` removes the wrappers, then unmounts strictly (refused while a process of the sandbox runs from it or has
a file of it open; --force detaches it anyway). A toolchain given at the start stays for the run.
docs/TOOLCHAINS_DESIGN.md has the format, the checks and the threat model.

Standard library only.
"""
import argparse
import contextlib
import ctypes
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
PROC = Path("/proc")
MOUNT = "/opt/toolchains"          # in the sandbox: the session's folder, read-only
BIN = MOUNT + "/bin"               # first on the agent's PATH (runtime.py: TOOLCHAINS_BIN)
ENV_FILE = MOUNT + "/env.sh"       # the toolchains' variables, for a shell to read (BASH_ENV, profile.d)
PROFILE_TARGET = "/etc/profile.d/sandbox-ai-toolchains.sh"  # Podman: a login shell's PATH (/etc/profile resets it)
SANDBOX_HOME = "/root"             # every sandbox's HOME
SCHEMA = "sandbox-ai-toolchains"
CATALOG = "https://raw.githubusercontent.com/c0decave/sandbox-ai-mcp/main/toolchains.json"
LOCAL_CATALOGS_VARIABLE = "SANDBOX_AI_LOCAL_CATALOGS"  # as mcp.py: =1 the copy in this tree, =FOLDER its checkout
CATALOG_LIMIT = 2 << 20
ARCHIVE_LIMIT = 2 << 30            # bytes of an archive (a release asset's limit)
RESERVE = 256 << 20                # bytes left free beside a toolchain that is added
NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
RESERVED = frozenset({"bin"})      # /opt/toolchains/bin is the wrappers' folder
VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")
COMMAND = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SONAME = re.compile(r"lib[A-Za-z0-9_+.-]{1,60}\.so(?:\.[0-9]{1,5}){0,3}")
GLIBC = re.compile(r"([0-9]{1,2})\.([0-9]{1,3})")
HOST = re.compile(r"(?:\*\.)?(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
                  r"[A-Za-z0-9])?")
PRESET = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
ARCHES = ("x86_64", "aarch64")
TEXT_KEYS = ("title", "description", "license", "homepage")
KEYS = frozenset({"name", "version", "arch", "url", "sha256", "size", "unpacked", "commands", "env", "caches",
                  "hosts", "allow", "needs", "requires", "sources", *TEXT_KEYS})
# Variables no toolchain sets: they decide which code every process loads, or belong to the sandbox itself.
ENV_REFUSED = re.compile(r"(LD_|DYLD_|SANDBOX_AI_).*|PATH|HOME|USER|SHELL|BASH_ENV|ENV|IFS|PS[1-4]|PROMPT_COMMAND|"
                         r"NODE_OPTIONS|PYTHONSTARTUP|PYTHONPATH|PYTHONHOME|PERL5OPT|PERL5LIB|RUBYOPT|RUBYLIB|"
                         r"JAVA_TOOL_OPTIONS|_JAVA_OPTIONS|JDK_JAVA_OPTIONS|GIT_.*|HTTPS?_PROXY|https?_proxy|"
                         r"ALL_PROXY|NO_PROXY|SSL_CERT_FILE|SSL_CERT_DIR|CURL_CA_BUNDLE|REQUESTS_CA_BUNDLE|"
                         r"NODE_EXTRA_CA_CERTS")
LIMITS = {"commands": 256, "env": 32, "caches": 16, "hosts": 64, "allow": 8, "libraries": 16, "requires": 4,
          "sources": 64}
# What sandbox-ai's own agent images have (Debian bookworm: node:lts-bookworm-slim, with openssl): what a
# toolchain's "needs" is checked against under Podman. An image of your own is not known (a note instead of a check).
OWN_IMAGE = {"glibc": "2.36", "libraries": frozenset({"libgcc_s.so.1", "libstdc++.so.6", "libatomic.so.1",
                                                     "libz.so.1", "libssl.so.3", "libcrypto.so.3"})}
# glibc's own libraries: never named in "needs.libraries" (the floor covers them).
GLIBC_LIBRARIES = frozenset({"libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1",
                             "libutil.so.1", "libresolv.so.2", "libanl.so.1", "libmvec.so.1",
                             "ld-linux-x86-64.so.2", "ld-linux-aarch64.so.1"})
STATES = ("attached", "removed")
SYS_MOVE_MOUNT = 429
MOVE_MOUNT_F_EMPTY_PATH, MOVE_MOUNT_T_EMPTY_PATH = 0x4, 0x40
CLONE_NEWNS, CLONE_NEWUSER = 0x00020000, 0x10000000
MNT_DETACH, UMOUNT_NOFOLLOW = 2, 8
UNSURE = 4  # the helper's exit status when an attach failed and could not be taken out again everywhere
BUSY = 5    # the helper's exit status when the kernel refuses the unmount: something of the sandbox holds the tree
PROFILE_TEXT = ("# sandbox-ai: the toolchains' commands on a login shell's PATH (sandbox-ai toolchain)\n"
                f'case ":${{PATH}}:" in *:{BIN}:*) ;; *) PATH="{BIN}:${{PATH}}"; export PATH ;; esac\n'
                f"[ -r {ENV_FILE} ] && . {ENV_FILE}\n")
_modules = {}


def module(name):
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_toolchains", HERE / f"{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules[name] = loaded
    return _modules[name]


def shown(value, limit=300):
    """VALUE as it may be printed: everything outside printable ASCII escaped (a catalog's text included)."""
    text = str(value)[:limit]
    return "".join(c if " " <= c <= "~" else c.encode("unicode_escape").decode("ascii") for c in text)


# --- the manifest: an entry of the catalog --------------------------------------------------------------------


def machine(value=None):
    """This machine's architecture as the catalog names it (x86_64, aarch64), or the raw name."""
    value = (value or platform.machine()).lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(value, value)


def spec(value):
    """(name, version or None) of NAME[@VERSION]."""
    name, at, version = str(value).partition("@")
    if not NAME.fullmatch(name) or name in RESERVED:
        raise ValueError(f"a toolchain's name is a lower-case letter, then at most 31 lower-case letters, digits or "
                         f"'-' (not {', '.join(sorted(RESERVED))}): {shown(name)!r}")
    if at and not VERSION.fullmatch(version):
        raise ValueError(f"{name}@{shown(version)}: a version is 1-64 letters, digits, '.', '+', '_' or '-'")
    return name, (version if at else None)


def below_prefix(value, top="the toolchain's prefix"):
    """VALUE (a path of a manifest) as a path below TOP, or ValueError."""
    path = PurePosixPath(str(value))
    if not isinstance(value, str) or not value or path.is_absolute() or len(value) > 512 or \
            any(part in ("", ".", "..") for part in path.parts) or not re.fullmatch(r"[A-Za-z0-9._+@/-]+", value):
        raise ValueError(f"{shown(value)!r} is no path below {top}")
    return path.as_posix()


def glibc_tuple(value):
    match = GLIBC.fullmatch(str(value)) if isinstance(value, str) else None
    return (int(match[1]), int(match[2])) if match else None


def texts(value, pattern, limit, what, where):
    if not isinstance(value, list) or len(value) > limit or not all(
            isinstance(item, str) and pattern.fullmatch(item) for item in value):
        raise ValueError(f"{where}: {what}")
    return list(dict.fromkeys(value))


def validated(entry, where="toolchain"):
    """The manifest ENTRY with every value checked (a new dict of the known keys), or ValueError. Nothing of it is
    trusted before: it becomes paths, wrapper scripts, allowed hosts and mounts."""
    if not isinstance(entry, dict):
        raise ValueError(f"{where}: an entry is an object")
    unknown = sorted(set(entry) - KEYS - {key for key in entry if key.startswith("_")})
    if unknown:
        raise ValueError(f"{where}: unknown key {shown(unknown[0])!r}")
    name, version = entry.get("name"), entry.get("version")
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in RESERVED:
        raise ValueError(f"{where}: name must be a lower-case letter, then lower-case letters, digits or '-' "
                         f"(at most 32; not {', '.join(sorted(RESERVED))})")
    where = f"{where}: {name}"
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError(f"{where}: version must be 1-64 letters, digits, '.', '+', '_' or '-'")
    if entry.get("arch") not in ARCHES:
        raise ValueError(f"{where}: arch is one of {', '.join(ARCHES)}")
    url = entry.get("url")
    if not isinstance(url, str) or len(url) > 2048 or any(c <= " " or c > "~" for c in url) or \
            not (url.startswith("https://") or url.startswith("/") or url.startswith("file:///")):
        raise ValueError(f"{where}: url must be an https URL (or a local file of a catalog of your own)")
    if not isinstance(entry.get("sha256"), str) or not SHA256.fullmatch(entry["sha256"]):
        raise ValueError(f"{where}: sha256 must be 64 hex digits")
    result = {"name": name, "version": version, "arch": entry["arch"], "url": url, "sha256": entry["sha256"]}
    for key in ("size", "unpacked"):
        value = entry.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 16 << 30:
            raise ValueError(f"{where}: {key} must be a number of bytes")
        result[key] = value
    for key in TEXT_KEYS:
        value = entry.get(key, "")
        if not isinstance(value, str) or len(value) > 1000:
            raise ValueError(f"{where}: {key} must be a text")
        result[key] = value
    commands = entry.get("commands")
    if not isinstance(commands, dict) or not commands or len(commands) > LIMITS["commands"] or not all(
            isinstance(key, str) and COMMAND.fullmatch(key) for key in commands):
        raise ValueError(f"{where}: commands must map command names (letters, digits, '.', '_', '+', '-') to files "
                         "below the prefix")
    try:
        result["commands"] = {key: below_prefix(value) for key, value in sorted(commands.items())}
    except ValueError as exc:
        raise ValueError(f"{where}: commands: {exc}") from None
    env = entry.get("env", {})
    if not isinstance(env, dict) or len(env) > LIMITS["env"] or not all(
            isinstance(key, str) and VARIABLE.fullmatch(key) and isinstance(value, str) and len(value) <= 1024
            and all(" " <= c <= "~" for c in value) for key, value in env.items()):
        raise ValueError(f"{where}: env must map variable names to printable text")
    caches = entry.get("caches", {})
    if not isinstance(caches, dict) or len(caches) > LIMITS["caches"] or not all(
            isinstance(key, str) and VARIABLE.fullmatch(key) for key in caches):
        raise ValueError(f"{where}: caches must map variable names to folders below the sandbox's HOME")
    for key in [*env, *caches]:
        if ENV_REFUSED.fullmatch(key):
            raise ValueError(f"{where}: a toolchain may not set {key}")
    if set(env) & set(caches):
        raise ValueError(f"{where}: {sorted(set(env) & set(caches))[0]} is in env and in caches")
    for key, value in env.items():
        rest = value.replace("{prefix}", "")
        if "{" in rest or "}" in rest:
            raise ValueError(f"{where}: env {key}: only {{prefix}} may stand in braces")
    result["env"] = dict(sorted(env.items()))
    try:
        result["caches"] = {key: below_prefix(value, "the sandbox's HOME") for key, value in sorted(caches.items())}
    except ValueError as exc:
        raise ValueError(f"{where}: caches: {exc}") from None
    result["hosts"] = texts(entry.get("hosts", []), HOST, LIMITS["hosts"],
                            "hosts must be host names (or *.DOMAIN)", where)
    result["allow"] = texts(entry.get("allow", []), PRESET, LIMITS["allow"],
                            "allow must name presets (github, pypi, npm ...)", where)
    needs = entry.get("needs", {})
    if not isinstance(needs, dict) or set(needs) - {"glibc", "libraries"}:
        raise ValueError(f"{where}: needs is {{glibc, libraries}}")
    floor = needs.get("glibc")
    if floor is not None and glibc_tuple(floor) is None:
        raise ValueError(f"{where}: needs.glibc must be a version such as 2.28 (or null: no C library needed)")
    libraries = texts(needs.get("libraries", []), SONAME, LIMITS["libraries"],
                      "needs.libraries must be library names such as libstdc++.so.6", where)
    result["needs"] = {"glibc": floor, "libraries": libraries}
    result["requires"] = texts(entry.get("requires", []), NAME, LIMITS["requires"],
                               "requires must name toolchains", where)
    if name in result["requires"]:
        raise ValueError(f"{where}: it requires itself")
    sources = entry.get("sources", [])
    if not isinstance(sources, list) or len(sources) > LIMITS["sources"] or not all(
            isinstance(item, dict) and isinstance(item.get("url"), str) and len(item["url"]) <= 2048
            and isinstance(item.get("sha256"), str) and SHA256.fullmatch(item["sha256"])
            and isinstance(item.get("signature", ""), str) and len(item.get("signature", "")) <= 300
            for item in sources):
        raise ValueError(f"{where}: sources must be a list of {{url, sha256[, signature]}}")
    result["sources"] = [{"url": item["url"], "sha256": item["sha256"],
                          **({"signature": item["signature"]} if item.get("signature") else {})} for item in sources]
    return result


# --- the catalog and its signature ----------------------------------------------------------------------------


def default_catalog(root=None, environ=None):
    """The published, signed catalog; the copy of a development checkout only with SANDBOX_AI_LOCAL_CATALOGS (as
    mcp.default_catalogs: its signature is checked like the published one's)."""
    environ = os.environ if environ is None else environ
    root = Path(root) if root else HERE.parent
    wanted = environ.get(LOCAL_CATALOGS_VARIABLE) or ""
    folder = Path(wanted) if os.path.isabs(wanted) else root / "projects"
    local = folder / "sandbox-ai-mcp" / "toolchains.json"
    return str(local) if (wanted == "1" or os.path.isabs(wanted)) and local.is_file() else CATALOG


def read_source(source, fetch=None):
    """The bytes of SOURCE: an https URL or a file (at most CATALOG_LIMIT + 1)."""
    if source.startswith("https://"):
        import urllib.request
        fetch = fetch or (lambda url: urllib.request.urlopen(url, timeout=30).read(CATALOG_LIMIT + 1))
        return fetch(source)
    if "://" in source and not source.startswith("file://"):
        raise ValueError(f"{shown(source)}: a catalog is an https URL or a file")
    path = source[len("file://"):] if source.startswith("file://") else source
    with open(os.path.expanduser(path), "rb") as stream:
        return stream.read(CATALOG_LIMIT + 1)


def catalog_signature(source, data, fetch=None, keys=None):
    """{"state": "good", "signer": FINGERPRINT} when SOURCE.sig is an SSH signature over DATA by a key of
    sandbox/mcp-signers (namespace sandbox-ai-mcp: the MCP catalog's own trust), else {"state": "unsigned"|"bad",
    "reason"}. It covers every archive's SHA-256 and every manifest."""
    k = module("release_keys")
    try:
        trusted = k.load(ring=k.MCP) if keys is None else keys
    except ValueError as exc:
        return {"state": "bad", "reason": str(exc)}
    try:
        signature = read_source(source + ".sig", fetch)
    except Exception:  # noqa: BLE001 - not there, not reachable: unsigned, which add refuses unless asked
        return {"state": "unsigned", "reason": f"{source}.sig cannot be read"}
    if not trusted:
        return {"state": "unsigned", "reason": f"no signing key is trusted yet ({k.MCP['signers']})"}
    try:
        verdict, detail = k.verify(data, signature, trusted, ring=k.MCP, what="toolchains.json")
    except RuntimeError as exc:  # no ssh-keygen
        return {"state": "unsigned", "reason": str(exc)}
    return {"state": "good", "signer": detail} if verdict == "good" else {"state": "bad", "reason": detail}


def read_catalog(source=None, fetch=None, keys=None):
    """(entries, signature, problems) of the catalog SOURCE: the well-formed entries of every architecture, each
    with "_signature" and "_catalog"; an entry that is not is named in PROBLEMS and left out."""
    source = source or default_catalog()
    try:
        data = read_source(source, fetch)
    except OSError as exc:
        if getattr(exc, "code", None) == 404:
            raise ValueError(f"{source}: not published there (yet); pass --catalog FILE_OR_URL") from None
        reason = getattr(exc, "reason", None)
        raise ValueError(f"{source}: {getattr(reason, 'strerror', None) or reason or exc.strerror or exc}") from None
    if len(data) > CATALOG_LIMIT:
        raise ValueError(f"{source}: larger than {CATALOG_LIMIT >> 20} MiB")
    try:
        document = json.loads(data)
    except (ValueError, RecursionError):
        raise ValueError(f"{source}: not JSON") from None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA or document.get("version") != 1 or \
            not isinstance(document.get("toolchains"), list):
        raise ValueError(f"{source}: not a toolchain catalog (schema {SCHEMA}, version 1)")
    signature = catalog_signature(source, data, fetch, keys)
    entries, problems = [], []
    for index, entry in enumerate(document["toolchains"][:1024]):
        try:
            entries.append({**validated(entry, f"entry {index + 1}"), "_signature": signature, "_catalog": source})
        except ValueError as exc:
            problems.append(f"{source}: {exc}: left out")
    return entries, signature, problems


def version_key(version):
    """VERSION for ordering: its numbers first (1.25.4 before 1.9), then its text."""
    return [int(part) for part in re.findall(r"[0-9]+", version)][:8], version


def find_entry(entries, name, version=None, arch=None):
    """The entry NAME (VERSION, else its newest) for ARCH (this machine's) of ENTRIES, or ValueError saying why."""
    arch = arch or machine()
    named = [entry for entry in entries if entry["name"] == name]
    if not named:
        known = ", ".join(sorted({entry["name"] for entry in entries})) or "none"
        raise ValueError(f"no toolchain {name} in the catalog (it has: {known})")
    here = [entry for entry in named if entry["arch"] == arch]
    if not here:
        raise ValueError(f"the catalog has {name} for {', '.join(sorted({e['arch'] for e in named}))}, not for this "
                         f"machine ({arch})")
    if version is not None:
        exact = [entry for entry in here if entry["version"] == version]
        if not exact:
            raise ValueError(f"the catalog has {name} {', '.join(sorted(e['version'] for e in here))}, not {version}")
        return exact[0]
    return max(here, key=lambda entry: version_key(entry["version"]))


def check_signature(entry, unsigned=False, out=print):
    """Refuse ENTRY unless its catalog is signed by a trusted key; UNSIGNED takes one of an unsigned catalog (your
    own), never one whose signature does not verify. The signer's fingerprint, or None (added unsigned)."""
    signature = entry.get("_signature") or {"state": "unsigned", "reason": "no signature was read"}
    if signature["state"] == "good":
        return signature["signer"]
    if signature["state"] == "bad":
        raise ValueError(f"{entry['name']}: the catalog's signature does not verify ({signature['reason']}); nothing "
                         "of it is used")
    if not unsigned:
        raise ValueError(f"{entry['name']}: its catalog is not signed by a trusted key ({signature['reason']}), so "
                         "nobody vouched that it is built from its pinned sources and works with sandbox-ai; "
                         "--unsigned adds it anyway")
    out(f"sandbox-ai toolchain: {entry['name']}: added unsigned ({signature['reason']})")
    return None


# --- what a toolchain needs from the system ------------------------------------------------------------------


def host_glibc():
    """(major, minor) of this host's glibc, or None (another C library, or not told)."""
    try:
        text = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (ValueError, OSError, AttributeError):
        return None
    match = re.fullmatch(r"glibc ([0-9]+)\.([0-9]+)(?:\..*)?", text.strip())
    return (int(match[1]), int(match[2])) if match else None


LIBRARY_DIRS = ("/lib64", "/usr/lib64", "/lib", "/usr/lib", "/usr/local/lib", "/usr/local/lib64")


def library_dirs(root=Path("/")):
    """The folders the host's loader searches: the standard ones, the multiarch ones, /etc/ld.so.conf's."""
    found = [root / path.lstrip("/") for path in LIBRARY_DIRS]
    for base in ("lib", "usr/lib"):
        with contextlib.suppress(OSError):
            found += [entry for entry in sorted((root / base).iterdir())
                      if entry.name.endswith("-linux-gnu") and entry.is_dir()]
    files = [root / "etc/ld.so.conf"]
    with contextlib.suppress(OSError):
        files += sorted((root / "etc/ld.so.conf.d").glob("*.conf"))
    for file in files[:64]:
        with contextlib.suppress(OSError, UnicodeDecodeError):
            for line in file.read_text().splitlines()[:256]:
                line = line.strip()
                if line.startswith("/"):
                    found.append(root / line.lstrip("/"))
    return list(dict.fromkeys(found))


def host_has_library(name, root=Path("/")):
    return any((directory / name).exists() for directory in library_dirs(root))


def needs_problem(entry, backend, own_image=True, glibc=host_glibc, has_library=host_has_library):
    """(why ENTRY cannot run in a sandbox of BACKEND here, a note or None): under Bubblewrap the host's glibc and
    libraries are the sandbox's; under Podman sandbox-ai's own images are known (OWN_IMAGE), an image of your own
    (OWN_IMAGE False) is not - a note then."""
    name, needs = f"{entry['name']} {entry['version']}", entry.get("needs") or {}
    floor, libraries = glibc_tuple(needs.get("glibc")), list(needs.get("libraries") or [])
    if entry.get("arch") != machine():
        return f"{name} is built for {entry.get('arch')}, this machine is {machine()}", None
    if backend == "podman":
        if not own_image:
            wanted = ([f"glibc {needs['glibc']} or newer"] if floor else []) + libraries
            return None, (f"{name} needs {', '.join(wanted)} in the image; an image of your own is not checked"
                          if wanted else None)
        have = glibc_tuple(OWN_IMAGE["glibc"])
        if floor and floor > have:
            return (f"{name} needs glibc {needs['glibc']} or newer, sandbox-ai's images have {OWN_IMAGE['glibc']}",
                    None)
        missing = [library for library in libraries if library not in OWN_IMAGE["libraries"]]
        return (f"{name} needs {', '.join(missing)}, which sandbox-ai's images do not have" if missing else None,
                None)
    have = glibc()
    if floor and have is None:
        return (f"{name} needs glibc {needs['glibc']} or newer; this host's C library is not glibc (with Bubblewrap "
                "the sandbox runs the host's own /usr): use Podman"), None
    if floor and have < floor:
        return (f"{name} needs glibc {needs['glibc']} or newer, this host has {have[0]}.{have[1]} (with Bubblewrap "
                "the sandbox runs the host's own /usr): use Podman, whose image has "
                f"{OWN_IMAGE['glibc']}"), None
    missing = [library for library in libraries if not has_library(library)]
    if missing:
        return (f"{name} needs {', '.join(missing)}, which this host does not have (with Bubblewrap the sandbox "
                "runs the host's own /usr): install it, or use Podman"), None
    return None, None


# --- the store ------------------------------------------------------------------------------------------------


def store():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "sandbox-ai" / "toolchains"


def tree(sha256):
    if not SHA256.fullmatch(str(sha256)):
        raise ValueError("a toolchain's tree is named by its SHA-256 (64 hex digits)")
    return store() / sha256


@contextlib.contextmanager
def store_lock():
    directory = store()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def installed():
    """{name: {version: manifest}} of the store (installed.json): only entries that are well-formed and whose tree
    is there; "signed_by" (a key's fingerprint, or None: added unsigned) and "added" with each."""
    try:
        data = json.loads((store() / "installed.json").read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    found = {}
    toolchains = data.get("toolchains") if isinstance(data, dict) else None
    for name, versions in (toolchains if isinstance(toolchains, dict) else {}).items():
        if not isinstance(versions, dict):
            continue
        for version, entry in versions.items():
            try:
                checked = validated({key: value for key, value in entry.items() if key in KEYS}, "installed.json")
            except (ValueError, AttributeError):
                continue
            if checked["name"] != name or checked["version"] != version or not tree(checked["sha256"]).is_dir():
                continue
            signer = entry.get("signed_by")
            found.setdefault(name, {})[version] = {
                **checked, "signed_by": signer if isinstance(signer, str) else None,
                "added": entry.get("added") if isinstance(entry.get("added"), str) else None,
                "path": str(tree(checked["sha256"]))}
    return found


def save_installed(found):
    path = store() / "installed.json"
    document = {"version": 1, "toolchains": {name: {version: {key: value for key, value in entry.items()
                                                              if key != "path"}
                                                    for version, entry in versions.items()}
                                             for name, versions in found.items() if versions}}
    temporary = path.with_name(".installed.json")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as stream:
        json.dump(document, stream, indent=1)
    os.replace(temporary, path)


def chosen(found, name, version=None):
    """The installed entry NAME (VERSION, else its newest), or ValueError naming the command that adds it."""
    versions = found.get(name) or {}
    if version is not None:
        if version not in versions:
            have = f" (installed: {', '.join(sorted(versions, key=version_key))})" if versions else ""
            raise ValueError(f"the toolchain {name} {version} is not in the store{have}: sandbox-ai toolchain add "
                             f"{name}@{version}")
        return versions[version]
    if not versions:
        raise ValueError(f"the toolchain {name} is not in the store: sandbox-ai toolchain add {name}")
    return versions[max(versions, key=version_key)]


def case_sensitive(directory):
    """Whether DIRECTORY's file system tells Name from name (macOS's usually does not)."""
    with tempfile.NamedTemporaryFile(prefix=".CaseProbe-", dir=directory) as probe:
        other = Path(probe.name).with_name(Path(probe.name).name.lower())
        return other.name == Path(probe.name).name or not other.exists()


def download(url, sha256, target, opener=None, limit=ARCHIVE_LIMIT, progress=None):
    """The archive URL (https, or a local file) written to the file TARGET while it is hashed: never held in
    memory. ValueError unless its SHA-256 is SHA256 (TARGET is removed then); its size."""
    digest, size = hashlib.sha256(), 0
    if url.startswith("https://"):
        if opener is None:
            import urllib.request
            opener = lambda u: urllib.request.urlopen(u, timeout=120)  # noqa: E731
        source = opener(url)
        final = getattr(source, "geturl", lambda: url)()
        if not str(final).startswith("https://"):
            raise ValueError(f"{url}: redirected to {shown(final)}, which is not https: not downloaded")
    elif url.startswith("/") or url.startswith("file:///"):
        source = open(url[len("file://"):] if url.startswith("file://") else url, "rb")
    else:
        raise ValueError(f"{shown(url)}: a toolchain comes from an https URL or a local file")
    try:
        with source, open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600),
                          "wb") as stream:
            while True:
                chunk = source.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ValueError(f"{url}: larger than {limit >> 30} GiB")
                digest.update(chunk)
                stream.write(chunk)
                if progress:
                    progress(size)
        if digest.hexdigest() != sha256:
            raise ValueError(f"{url}: SHA-256 {digest.hexdigest()}, the signed catalog says {sha256}: not used")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(target)
        raise
    return size


def install(entry, unsigned=False, out=print, opener=None):
    """ENTRY (of read_catalog) into the store: its signature checked, its archive downloaded and checked against its
    SHA-256, unpacked without trusting it (mcp_rootfs.unpack), every command's file found in the tree. The installed
    entry; one that is there already is only named."""
    rootfs = module("mcp_rootfs")
    signer = check_signature(entry, unsigned, out)
    label = f"{entry['name']} {entry['version']}"
    with store_lock():
        found = installed()
        target = tree(entry["sha256"])
        if not target.is_dir():
            wanted = entry.get("size", 0) + entry.get("unpacked", 0) + RESERVE
            free = shutil.disk_usage(store()).free
            if wanted > RESERVE and free < wanted:
                raise ValueError(f"{label} needs {wanted >> 20} MiB in {store()} (its archive, its files and a "
                                 f"reserve); {free >> 20} MiB are free")
            out(f"sandbox-ai toolchain: downloading {label}"
                + (f" ({entry['size'] >> 20} MiB)" if entry.get("size") else "") + f" from {shown(entry['url'])}")
            archive = store() / f".download-{os.getpid()}-{entry['sha256'][:12]}"
            with contextlib.suppress(OSError):
                os.unlink(archive)
            download(entry["url"], entry["sha256"], archive, opener)
            try:
                out(f"sandbox-ai toolchain: unpacking {label}")
                with open(archive, "rb") as stream:
                    rootfs.unpack(stream, target, case_sensitive=case_sensitive(store()))
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(archive)
        missing = [path for path in entry["commands"].values() if not (target / path).is_file()]
        if missing:
            if not any(target == tree(other["sha256"]) for versions in found.values() for other in versions.values()):
                rootfs.remove(target)
            raise ValueError(f"{label}: its archive has no {missing[0]}, which its manifest names as a command: "
                             "not added")
        record = {**{key: value for key, value in entry.items() if key in KEYS}, "signed_by": signer,
                  "added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        found.setdefault(entry["name"], {})[entry["version"]] = record
        save_installed(found)
    return {**record, "path": str(target)}


def add(value, catalog=None, unsigned=False, out=print, fetch=None, opener=None, keys=None):
    """`toolchain add NAME[@VERSION]`: the entry and the toolchains it requires, installed."""
    name, version = spec(value)
    entries, _, problems = read_catalog(catalog, fetch, keys)
    for problem in problems:
        out(f"sandbox-ai toolchain: {shown(problem, 500)}")
    done, pending = [], [(name, version)]
    while pending:
        wanted, at = pending.pop(0)
        if wanted in [entry["name"] for entry in done]:
            continue
        entry = find_entry(entries, wanted, at)
        have = installed().get(wanted, {}).get(entry["version"])
        if have and have["sha256"] == entry["sha256"]:
            out(f"{wanted} {entry['version']} is in the store already")
            done.append(have)
        else:
            done.append(install(entry, unsigned, out, opener))
            out(f"added {wanted} {entry['version']} ({', '.join(entry['commands'])}): sandbox-ai AGENT --toolchain "
                f"{wanted}, or into a running sandbox: sandbox-ai toolchain attach RUN {wanted}")
        pending += [(other, None) for other in entry["requires"]]
    return done


def users_of(sha256, runs=None):
    """The names of the running sandboxes that have the tree SHA256 mounted."""
    net = module("net")
    runs = net.running() if runs is None else runs
    return [str(record.get("name") or run) for run, record in runs.items()
            if any(entry.get("sha256") == sha256 and entry.get("state") == "attached" for entry in load(record))]


def remove(value, out=print, runs=None):
    """`toolchain remove NAME[@VERSION]`: every version of NAME, or the one; refused while a sandbox uses one."""
    name, version = spec(value)
    with store_lock():
        found = installed()
        versions = found.get(name) or {}
        if version is not None and version not in versions or not versions:
            raise ValueError(f"the toolchain {value} is not in the store")
        for at in [version] if version is not None else sorted(versions, key=version_key):
            entry = versions[at]
            using = users_of(entry["sha256"], runs)
            if using:
                raise ValueError(f"{name} {at} is mounted in {', '.join(map(shown, using))}: detach it there "
                                 f"(sandbox-ai toolchain detach RUN {name}) or stop the sandbox first")
            del versions[at]
            save_installed(found)
            if not any(other["sha256"] == entry["sha256"] for all_versions in found.values()
                       for other in all_versions.values()):
                module("mcp_rootfs").remove(tree(entry["sha256"]))
            out(f"removed {name} {at} from the store")


# --- the session's folder: the wrappers and the variables ------------------------------------------------------


def session_folder(session_dir):
    """The host's folder mounted at /opt/toolchains: toolchains/ of the run's session directory."""
    return Path(session_dir) / "toolchains"


def expanded(value, name):
    return value.replace("{prefix}", f"{MOUNT}/{name}")


def variables(entry):
    """[(variable, value for a shell: quoted, ${HOME} left to it)] of ENTRY: its env, then its caches (in HOME)."""
    name = entry["name"]
    lines = [(key, shlex.quote(expanded(value, name))) for key, value in entry.get("env", {}).items()]
    lines += [(key, '"${HOME}"/' + shlex.quote(path)) for key, path in entry.get("caches", {}).items()]
    return lines


def environment(entries):
    """{variable: value} of ENTRIES for the agent's environment at a start (the first toolchain that names a variable
    keeps it); the caches below the sandbox's HOME."""
    env = {}
    for entry in entries:
        for key, value in entry.get("env", {}).items():
            env.setdefault(key, expanded(value, entry["name"]))
        for key, path in entry.get("caches", {}).items():
            env.setdefault(key, f"{SANDBOX_HOME}/{path}")
    return env


def default_lines(entry):
    return [f'[ -n "${{{key}+x}}" ] || export {key}={value}' for key, value in variables(entry)]


def wrapper(entry, command):
    """The wrapper of COMMAND: the toolchain's variables where they are not set, then the program itself (by its
    path, so that it finds its own files and sees its own name)."""
    name = entry["name"]
    program = shlex.quote(f"{MOUNT}/{name}/{entry['commands'][command]}")
    return "\n".join(["#!/bin/sh", f"# sandbox-ai: {command} of the toolchain {name} {entry['version']}",
                      *default_lines(entry), f'exec {program} "$@"', ""])


def commands_of(entries):
    """({command: entry} - the first toolchain that names a command keeps it -, notes on the others)."""
    owners, notes = {}, []
    for entry in entries:
        for command in entry["commands"]:
            if command in owners:
                other = owners[command]
                notes.append(f"{command} stays {other['name']}'s; {entry['name']}'s is "
                             f"{MOUNT}/{entry['name']}/{entry['commands'][command]}")
            else:
                owners[command] = entry
    return owners, notes


def write_file(path, text, mode):
    """TEXT at PATH, replaced in one step (a process that reads it sees the old or the new, never half)."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as stream:
        stream.write(text)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def write_session(folder, entries):
    """FOLDER (session_folder) for the attached ENTRIES, in their order: bin/ with one wrapper per command, env.sh,
    profile.sh, and an empty folder NAME as each mount point. Wrappers of what is no longer attached go; a mount
    point stays (something may still be mounted on it). The notes on commands two toolchains name."""
    folder = Path(folder)
    for directory in (folder, folder / "bin"):
        directory.mkdir(mode=0o755, exist_ok=True)
        os.chmod(directory, 0o755)
    for entry in entries:
        (folder / entry["name"]).mkdir(mode=0o755, exist_ok=True)
    owners, notes = commands_of(entries)
    wanted = {command: wrapper(entry, command) for command, entry in owners.items()}
    for path in sorted((folder / "bin").iterdir()):
        if path.name not in wanted:
            with contextlib.suppress(OSError):
                path.unlink()
    for command, text in wanted.items():
        path = folder / "bin" / command
        try:
            same = path.read_text() == text
        except (OSError, UnicodeDecodeError):
            same = False
        if not same:
            write_file(path, text, 0o755)
    lines = ["# sandbox-ai: the attached toolchains' variables, read by new shells (sandbox-ai toolchain)",
             f'case ":${{PATH}}:" in *:{BIN}:*) ;; *) PATH="{BIN}:${{PATH}}"; export PATH ;; esac']
    for entry in entries:
        lines += [f"# {entry['name']} {entry['version']}", *default_lines(entry)]
    write_file(folder / "env.sh", "\n".join(lines) + "\n", 0o644)
    profile = folder / "profile.sh"
    if not profile.is_file() or profile.read_text() != PROFILE_TEXT:
        # Written once and in place: Podman mounts this very file at PROFILE_TARGET (a replaced file would not show).
        with open(profile, "w") as stream:
            stream.write(PROFILE_TEXT)
        os.chmod(profile, 0o644)
    return notes


# --- a start: --toolchain -------------------------------------------------------------------------------------


def resolved(values, found=None):
    """The installed entries of the --toolchain VALUES and of what they require, each name once (two versions of one
    toolchain share a mount point: refused), in the order given, a required one before the one that needs it."""
    found = installed() if found is None else found
    result = []

    def take(value, needed_by=None, seen=()):
        name, version = spec(value)
        if name in seen:
            raise ValueError(f"the toolchains {' and '.join(seen)} require each other")
        try:
            entry = chosen(found, name, version)
        except ValueError as exc:
            raise ValueError(f"{exc}" + (f" ({needed_by} requires it)" if needed_by else "")) from None
        for other in result:
            if other["name"] == name:
                if version is not None and other["version"] != version:
                    raise ValueError(f"--toolchain {name}@{other['version']} and {name}@{version}: one version of a "
                                     f"toolchain per sandbox (both would be {MOUNT}/{name})")
                return
        for required in entry["requires"]:
            take(required, name, (*seen, name))
        result.append(entry)
    for value in values:
        take(value)
    return result


def start_plan(values, backend, own_image=True, strict=False, offline=False, mode=None, found=None, presets=(),
               glibc=host_glibc, has_library=host_has_library):
    """What the start does with --toolchain VALUES: {"entries": [the installed entries], "hosts": [...],
    "presets": [...] (opened with them), "closed": [...] (hosts and presets left closed), "notes": [...]}.
    ValueError for one that is not in the store or cannot run here. STRICT (the modes high and paranoid) and OFFLINE
    (--toolchain-offline, --offline) open nothing. PRESETS: the launcher's own (an unknown name is left out)."""
    entries, notes = resolved(values, found), []
    for entry in entries:
        problem, note = needs_problem(entry, backend, own_image, glibc, has_library)
        if problem:
            raise ValueError(f"--toolchain {entry['name']}: {problem}")
        if note:
            notes.append(f"note: toolchain {note}")
        if not entry.get("signed_by"):
            notes.append(f"warning: the toolchain {entry['name']} {entry['version']} was added unsigned: nobody "
                         "vouched for what it runs in the sandbox")
    hosts = list(dict.fromkeys(host for entry in entries for host in entry["hosts"]))
    wanted = list(dict.fromkeys(preset for entry in entries for preset in entry["allow"]))
    unknown = [preset for preset in wanted if presets and preset not in presets]
    wanted = [preset for preset in wanted if preset not in unknown]
    for preset in unknown:
        notes.append(f"note: a toolchain names the preset {preset}, which this sandbox-ai does not have: left out")
    closed = []
    if (strict or offline) and (hosts or wanted):
        closed = [*hosts, *(f"--allow-{preset}" for preset in wanted)]
        why = f"security mode {mode}" if strict else "offline"
        names = ", ".join(entry["name"] for entry in entries if entry["hosts"] or entry["allow"])
        notes.append(f"note: {why}: the registries of {names} stay closed ({', '.join(closed)})"
                     + (": a connection there is asked or refused as the mode does with any other" if strict else ""))
        hosts, wanted = [], []
    elif hosts or wanted:
        names = ", ".join(entry["name"] for entry in entries if entry["hosts"] or entry["allow"])
        opened = ", ".join([*hosts, *(f"--allow-{preset}" for preset in wanted)])
        notes.append(f"note: the registries of {names} are opened: {opened} (--toolchain-offline mounts the "
                     "toolchains without)")
    return {"entries": entries, "hosts": hosts, "presets": wanted, "closed": closed, "notes": notes}


def start_mounts(entries):
    """[(the store's tree, its place in the sandbox)] of a start's toolchains (read-only)."""
    return [(tree(entry["sha256"]), f"{MOUNT}/{entry['name']}") for entry in entries]


def plan_shown(plan):
    """A start's toolchains as the dry-run plan and run.json show them."""
    return [{"name": entry["name"], "version": entry["version"], "sha256": entry["sha256"],
             "sandbox": f"{MOUNT}/{entry['name']}", "mode": "ro", "commands": sorted(entry["commands"]),
             "signed_by": entry.get("signed_by")} for entry in plan["entries"]] if plan else []


# --- the run's toolchains: toolchains.json, toolchains.jsonl ---------------------------------------------------


def registry(record):
    return Path(record["directory"]) / "toolchains.json"


@contextlib.contextmanager
def locked(record):
    fd = os.open(Path(record["directory"]) / "toolchains.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                 | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load(record):
    """[{"name", "version", "sha256", "source" (start, added), "state", "since", "manifest", "opened"}]."""
    try:
        data = json.loads(registry(record).read_text())
    except (OSError, ValueError, RecursionError, KeyError, TypeError):
        return []
    entries = data.get("toolchains") if isinstance(data, dict) else None
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("state") in STATES
            and isinstance(entry.get("name"), str) and isinstance(entry.get("manifest"), dict)] \
        if isinstance(entries, list) else []


def save(record, entries):
    path = registry(record)
    temporary = path.with_name(".toolchains.json")
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), "w") as stream:
        json.dump({"version": 1, "toolchains": entries}, stream)
    os.replace(temporary, path)


def registered(entry, source, opened=()):
    manifest = {key: value for key, value in entry.items() if key in KEYS}
    return {"name": entry["name"], "version": entry["version"], "sha256": entry["sha256"], "source": source,
            "state": "attached", "since": time.time(), "manifest": manifest, "signed_by": entry.get("signed_by"),
            "opened": list(opened)}


def record_start(directory, plan, log_dir=None, run=None):
    """The start's toolchains into the run's registry (the launcher, once the run's directory is there), and into
    toolchains.jsonl of its log directory: what was mounted, and which registries were opened or left closed."""
    if plan and plan["entries"]:
        entries = [registered(entry, "start") for entry in plan["entries"]]
        save({"directory": str(directory)}, entries)
        record = {"log_dir": str(log_dir) if log_dir else None, "run": run}
        for entry in entries:
            log(record, "start", entry)
        if plan.get("hosts") or plan.get("presets") or plan.get("closed"):
            log(record, "open" if not plan.get("closed") else "closed", None,
                names=[entry["name"] for entry in entries], hosts=plan.get("hosts") or None,
                presets=plan.get("presets") or None, closed=plan.get("closed") or None)


def attached(record):
    return [entry for entry in load(record) if entry["state"] == "attached"]


def log(record, event, entry=None, **fields):
    """One line of toolchains.jsonl in the run's log directory (none without one)."""
    directory = record.get("log_dir")
    if not directory:
        return
    line = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, "run": record.get("run")}
    if entry:
        line.update(name=entry.get("name"), version=entry.get("version"), sha256=entry.get("sha256"),
                    source=entry.get("source"), signed_by=entry.get("signed_by"))
    line.update({key: value for key, value in fields.items() if value is not None})
    try:
        fd = os.open(Path(directory) / "toolchains.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o600)
        with open(fd, "a") as stream:
            stream.write(json.dumps(line) + "\n")
    except OSError:
        pass


# --- the namespace helper (runs in a process of its own: setns needs a single thread) ---------------------------


def identity(path):
    """[device, inode] of the folder PATH itself (never through a link)."""
    info = os.lstat(path)
    return [info.st_dev, info.st_ino]


def mount_point(name, base_identity):
    """(fd of /opt/toolchains, fd of /opt/toolchains/NAME) in the current mount namespace, neither through a link.
    /opt/toolchains must be the session's folder (BASE_IDENTITY: device, inode): a sandbox started before toolchains
    has none, and nothing else is ever mounted on."""
    try:
        base = os.open(MOUNT, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise OSError(errno.ENOENT, f"the sandbox has no {MOUNT} of its own (started by a sandbox-ai without "
                                    "toolchains?): restart it with --toolchain") from None
    try:
        info = os.fstat(base)
        if [info.st_dev, info.st_ino] != list(base_identity):
            raise OSError(errno.ESTALE, f"{MOUNT} in the sandbox is not the folder of its session: nothing is "
                                        "mounted there (restart it with --toolchain)")
        target = os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=base)
    except BaseException:
        os.close(base)
        raise
    return base, target


def covered(target, placeholder):
    """Something is mounted on the mount point: what opens there is not the session's empty folder (PLACEHOLDER)."""
    info = os.fstat(target)
    return [info.st_dev, info.st_ino] != list(placeholder)


def mount_at(c, name, tree_fd, base_identity, placeholder):
    ws = module("workspaces")
    base, target = mount_point(name, base_identity)
    try:
        if covered(target, placeholder):
            raise OSError(errno.EBUSY, f"{MOUNT}/{name} is mounted in the sandbox already")
        ws.check(c.syscall(SYS_MOVE_MOUNT, tree_fd, b"", target, b"", MOVE_MOUNT_F_EMPTY_PATH
                           | MOVE_MOUNT_T_EMPTY_PATH), f"mount {MOUNT}/{name}")
    finally:
        os.close(target)
        os.close(base)


def unmount_at(c, name, base_identity, placeholder, force=False):
    """Unmount /opt/toolchains/NAME of the current mount namespace: strictly first (the kernel refuses while a
    process runs from it or holds a file of it), FORCE detaches it then."""
    ws = module("workspaces")
    for _ in range(8):  # (an attach never stacks two; were there more, each goes)
        base, target = mount_point(name, base_identity)
        try:
            busy = covered(target, placeholder)
        finally:
            os.close(target)
            os.close(base)
        if not busy:
            return
        path = f"{MOUNT}/{name}".encode()
        if c.umount2(path, UMOUNT_NOFOLLOW) < 0:
            code = ctypes.get_errno()
            if code != errno.EBUSY:
                ctypes.set_errno(code)
                ws.check(-1, f"unmount {MOUNT}/{name}")
            if not force:
                raise BlockingIOError(errno.EBUSY, f"{MOUNT}/{name} is in use in the sandbox (a process runs from "
                                                   "it or has a file of it open): --force detaches it anyway")
            ws.check(c.umount2(path, MNT_DETACH | UMOUNT_NOFOLLOW), f"unmount {MOUNT}/{name}")


def helper(request, proc=PROC, c=None):
    """In this (fresh, single-threaded) process: enter the user namespace of REQUEST's "user", then mount "source"
    (op add: the store's tree, still "identity") read-only at /opt/toolchains/"name", or unmount it there (op
    remove), in the mount namespace of every pid of "targets". "starts": the processes' start times when they were
    found (a PID that names another process now is refused). An add that fails in one namespace is taken out of
    those it reached (exit status UNSURE when that fails too)."""
    ws = module("workspaces")
    c = c or ws.libc()
    op, name, targets = request["op"], request["name"], [int(pid) for pid in request["targets"]]
    if op not in ("add", "remove") or not NAME.fullmatch(str(name)) or name in RESERVED:
        raise OSError(errno.EINVAL, "not a toolchain request")
    user_pid, starts = int(request["user"]), request.get("starts") or {}
    user = os.open(Path(proc) / str(user_pid) / "ns" / "user", os.O_RDONLY | os.O_CLOEXEC)
    namespaces = [os.open(Path(proc) / str(pid) / "ns" / "mnt", os.O_RDONLY | os.O_CLOEXEC) for pid in targets]
    for pid in [user_pid, *targets]:  # after the namespaces were opened: what the descriptors hold is that process's
        if ws.start_time(pid, proc) is None or ws.start_time(pid, proc) != starts.get(str(pid)):
            raise OSError(errno.ESRCH, f"process {pid} of the sandbox is gone (its PID may name another process "
                                       "now): try again")
    ws.check(c.setns(user, CLONE_NEWUSER), "enter the sandbox's user namespace")
    trees = []
    if op == "add":
        # A mount namespace of our own, owned by the sandbox's user namespace, may clone the host's folder; each
        # namespace needs a clone of its own. Read-only, nosuid, nodev (workspaces.clone: without mount_setattr,
        # nothing).
        ws.check(c.unshare(CLONE_NEWNS), "a private mount namespace")
        trees = [ws.clone(c, request["source"], request["identity"], "ro") for _ in namespaces]
    reached = []
    try:
        for index, fd in enumerate(namespaces):
            ws.check(c.setns(fd, CLONE_NEWNS), "enter a mount namespace of the sandbox")
            if op == "add":
                mount_at(c, name, trees[index], request["base"], request["placeholder"])
            else:
                unmount_at(c, name, request["base"], request["placeholder"], bool(request.get("force")))
            reached.append(index)
    except OSError as exc:
        if op != "add" or not reached:
            raise
        failed = False
        for index in reached:
            try:
                ws.check(c.setns(namespaces[index], CLONE_NEWNS), "enter a mount namespace of the sandbox")
                unmount_at(c, name, request["base"], request["placeholder"], True)
            except OSError:
                failed = True
        if failed:
            raise ws.Unsure(exc.errno or errno.EIO, f"{exc.strerror or exc}; and it could not be taken out again of "
                                                    "every mount namespace it reached") from None
        raise
    return 0


def run_helper(op, found, name, source=None, tree_identity=None, base=None, placeholder=None, force=False,
               proc=PROC, runner=None):
    """Run helper() in a process of its own; (status, error text). Only the sandbox's own mount namespaces (never
    one the agent made), each PID with its start time."""
    ws = module("workspaces")
    targets = ws.own_namespaces(found, proc)
    if not targets:
        return 1, "no mount namespace of the sandbox found"
    starts = {str(pid): ws.start_time(pid, proc) for pid in [found["user"], *targets]}
    gone = [pid for pid, value in starts.items() if value is None]
    if gone:
        return 1, f"process {gone[0]} of the sandbox is gone (has it ended?)"
    request = {"op": op, "user": found["user"], "name": name, "targets": targets, "source": source,
               "identity": tree_identity, "base": base, "placeholder": placeholder, "force": force, "starts": starts}
    command = [sys.executable, "-I", str(Path(__file__)), "--internal-helper", json.dumps(request)]
    try:
        result = (runner or subprocess.run)(command, capture_output=True, text=True, timeout=60,
                                            stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return UNSURE if op == "add" else 1, "the helper did not finish within 60 s"
    return result.returncode, result.stderr.strip()[-500:]


def helper_main(argument):
    try:
        return helper(json.loads(argument))
    except BlockingIOError as exc:
        print(exc.strerror or str(exc), file=sys.stderr)
        return BUSY
    except OSError as exc:
        print(exc.strerror or str(exc), file=sys.stderr)
        return UNSURE if isinstance(exc, module("workspaces").Unsure) else 1


# --- attach, detach, list ---------------------------------------------------------------------------------------


def finder_default(record):
    return module("devices").sandbox_processes(record)


def check_policy(launcher=None):
    launcher = launcher or module("agent_sandbox")
    if any(str(entry).split("=", 1)[0] == "--toolchain" for entry in launcher.load_policy().get("forbid", [])):
        raise ValueError(f"--toolchain is not allowed by the administrator's policy ({launcher.POLICY_FILE})")


def open_network(record, hosts, presets, out=print, net_main=None):
    """Open HOSTS and PRESETS for the running sandbox as `sandbox-ai net allow` does (its checks: the run's security
    mode, the administrator's policy, the supervisor's own); True when they are open."""
    if not hosts and not presets:
        return True
    net_main = net_main or module("net").main
    argv = ["allow", str(record.get("run")), *(part for host in hosts for part in ("--allow-host", host)),
            *(f"--allow-{preset}" for preset in presets)]
    return net_main(argv, out=out, brief=True, prog="toolchain attach") == 0


def session_of(record):
    return module("workspaces").session_of(record)


def keep(record, value, add=True):
    """--toolchain VALUE in the saved options of the run's session (a restart starts with it), or taken out again."""
    name = session_of(record)
    if name is None:
        return False
    sessions = module("sessions")
    with sessions.session_lock(name):
        data = sessions.load(name)
        options = list(data.get("options", []))
        pairs = [i for i in range(len(options) - 1) if options[i] == "--toolchain"
                 and options[i + 1].partition("@")[0] == value.partition("@")[0]]
        if add and not pairs:
            options += ["--toolchain", value]
        elif not add and pairs:
            del options[pairs[-1]:pairs[-1] + 2]
        else:
            return bool(pairs) and add
        data["options"] = options
        sessions.save(name, data)
    return True


def attach(record, value, offline=False, out=print, runner=None, finder=finder_default, found=None, launcher=None,
           net_main=None, glibc=host_glibc, has_library=host_has_library, own_image=None, keeper=keep):
    """`toolchain attach RUN NAME[@VERSION]`: the installed toolchain (and what it requires) mounted into the running
    sandbox RECORD, its commands on PATH, its registries opened. The names attached."""
    launcher = launcher or module("agent_sandbox")
    check_policy(launcher)
    name, version = spec(value)
    if launcher.machine_record(record):
        raise ValueError(f"{shown(record.get('run'))} runs in podman machine's VM: a toolchain cannot be mounted "
                         f"into it while it runs. Start it with the toolchain: sandbox-ai resume NAME --restart "
                         f"--toolchain {value}")
    if record.get("podman_url"):
        raise ValueError(f"{shown(record.get('run'))} runs on another Podman service (--podman-url): start it with "
                         f"--toolchain {value}")
    folder = session_folder(record.get("session_dir", ""))
    if not (folder / "bin").is_dir():
        raise ValueError(f"{shown(record.get('run'))} was started by a sandbox-ai without toolchains (it has no "
                         f"{MOUNT}): restart it with --toolchain {value}")
    have = attached(record)
    wanted = [entry for entry in resolved([value], found)
              if not any(other["name"] == entry["name"] for other in have)]
    for other in have:
        if other["name"] == name and (version is None or other["version"] == version):
            raise ValueError(f"{name} {other['version']} is attached to {shown(record.get('run'))} already")
        if other["name"] == name:
            raise ValueError(f"{name} {other['version']} is attached to {shown(record.get('run'))}: detach it first "
                             f"(one version of a toolchain per sandbox)")
    if own_image is None:
        own_image = not record.get("custom_image")
    for entry in wanted:
        problem, note = needs_problem(entry, record.get("backend"), own_image, glibc, has_library)
        if problem:
            log(record, "refuse", {**entry, "source": "added"}, reason=problem)
            raise ValueError(problem)
        if note:
            out(f"  note: {note}")
        if not entry.get("signed_by"):
            out(f"  warning: {entry['name']} {entry['version']} was added unsigned: nobody vouched for what it runs "
                "in the sandbox")
    processes = finder(record)
    if not processes:
        raise ValueError("the sandbox's processes were not found (has it ended?)")
    done = []
    with locked(record):
        entries = load(record)
        for entry in wanted:
            if any(other["name"] == entry["name"] and other["state"] == "attached" for other in entries):
                continue
            source = tree(entry["sha256"])
            point = folder / entry["name"]
            point.mkdir(mode=0o755, exist_ok=True)
            if point.is_symlink() or any(point.iterdir()):
                raise ValueError(f"{point} is not the empty folder a toolchain is mounted on")
            status, error = run_helper("add", processes, entry["name"], str(source), identity(source),
                                       identity(folder), identity(point), runner=runner)
            if status:
                log(record, "error", {**entry, "source": "added"}, reason=error)
                raise ValueError(f"could not attach {entry['name']}: {error or 'the helper failed'}"
                                 + ("; it may still be mounted in a part of the sandbox: sandbox-ai toolchain "
                                    f"detach {shown(record.get('run'))} {entry['name']} --force"
                                    if status == UNSURE else ""))
            item = registered(entry, "added")
            entries = [other for other in entries if other["name"] != entry["name"]] + [item]
            save(record, entries)
            log(record, "attach", item)
            done.append(item)
        if not done:
            raise ValueError(f"{name} is attached to {shown(record.get('run'))} already")
        active = [entry["manifest"] for entry in entries if entry["state"] == "attached"]
        for note in write_session(folder, active):
            out(f"  note: {note}")
    for item in done:
        entry = item["manifest"]
        out(f"attached {entry['name']} {entry['version']} to {shown(record.get('run'))} at {MOUNT}/{entry['name']}, "
            f"read-only: {', '.join(sorted(entry['commands']))}")
    hosts = list(dict.fromkeys(host for item in done for host in item["manifest"]["hosts"]))
    presets = [preset for preset in dict.fromkeys(p for item in done for p in item["manifest"]["allow"])
               if preset in launcher.PRESETS]
    if hosts or presets:
        shown_hosts = ", ".join([*hosts, *(f"--allow-{preset}" for preset in presets)])
        if offline:
            out(f"  --offline: its registries stay closed ({shown_hosts})")
        elif open_network(record, hosts, presets, out, net_main):
            with locked(record):
                entries = load(record)
                for entry in entries:
                    if entry["state"] == "attached" and entry["name"] in [item["name"] for item in done]:
                        entry["opened"] = [*entry["manifest"]["hosts"],
                                           *(f"--allow-{p}" for p in entry["manifest"]["allow"] if p in presets)]
                save(record, entries)
            log(record, "open", None, names=[item["name"] for item in done], hosts=hosts, presets=presets)
            out(f"  opened its registries: {shown_hosts} (take them back: sandbox-ai net revoke)")
        else:
            log(record, "closed", None, names=[item["name"] for item in done], hosts=hosts, presets=presets)
            out(f"  its registries stay closed ({shown_hosts}): what the sandbox may reach did not change (the "
                "reason above); the toolchain is mounted and works without them")
    if keeper(record, value):
        out("  kept in the session's options: a restart starts with it")
    out("  commands the agent starts from now on find it; a shell that is open reads its variables with: "
        f". {ENV_FILE}")
    return [item["name"] for item in done]


def detach(record, value, force=False, out=print, runner=None, finder=finder_default, keeper=keep):
    """`toolchain detach RUN NAME`: the wrappers first (nothing new starts from it), then the unmount."""
    name, _ = spec(value)
    with locked(record):
        entries = load(record)
        matches = [entry for entry in entries if entry["name"] == name and entry["state"] == "attached"]
        if not matches:
            raise ValueError(f"{name} is not attached to {shown(record.get('run'))}")
        entry = matches[0]
        if entry.get("source") != "added":
            raise ValueError(f"{name} was given at the start of {shown(record.get('run'))} and stays for the run "
                             "(toolchains attached during the run can be detached)")
        needing = [other["name"] for other in entries if other["state"] == "attached"
                   and name in (other["manifest"].get("requires") or [])]
        if needing:
            raise ValueError(f"{', '.join(needing)} requires {name}: detach it first")
        processes = finder(record)
        if not processes:
            raise ValueError("the sandbox's processes were not found (has it ended?)")
        folder = session_folder(record["session_dir"])
        rest = [other["manifest"] for other in entries if other["state"] == "attached" and other is not entry]
        write_session(folder, rest)
        status, error = run_helper("remove", processes, name, base=identity(folder),
                                   placeholder=identity(folder / name), force=force, runner=runner)
        if status:
            # Still mounted: its commands come back (nothing is half-detached).
            write_session(folder, [other["manifest"] for other in entries if other["state"] == "attached"])
            log(record, "error", entry, reason=error)
            raise ValueError(f"could not detach {name}: {error or 'the helper failed'}")
        entry.update(state="removed", since=time.time())
        save(record, entries)
    with contextlib.suppress(OSError):
        (folder / name).rmdir()
    log(record, "detach", entry, force=force or None)
    keeper(record, name, False)
    out(f"detached {name} from {shown(record.get('run'))}"
        + (f"; the hosts opened with it stay open until the run ends (sandbox-ai net revoke "
           f"{shown(record.get('run'))} ...)" if entry.get("opened") else ""))


def run_listing(record):
    """A running sandbox's toolchains as `toolchain list --run RUN --json` shows them."""
    return [{"name": entry["name"], "version": entry["version"], "source": entry.get("source"),
             "state": entry["state"], "sandbox": f"{MOUNT}/{entry['name']}", "sha256": entry["sha256"],
             "commands": sorted(entry["manifest"].get("commands") or {}), "opened": entry.get("opened") or [],
             "signed_by": entry.get("signed_by"),
             "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry.get("since") or 0))}
            for entry in load(record) if entry["state"] == "attached"]


def store_listing(found=None, runs=None):
    """The store as `toolchain list --json` shows it: one item per installed version, with the running sandboxes
    that have it mounted."""
    found = installed() if found is None else found
    if runs is None:
        try:
            runs = module("net").running()
        except Exception:  # noqa: BLE001 - the store is listed all the same
            runs = {}
    return [{"name": name, "version": version, "arch": entry["arch"], "title": entry["title"],
             "description": entry["description"], "license": entry["license"], "sha256": entry["sha256"],
             "size": entry["unpacked"] or entry["size"], "commands": sorted(entry["commands"]),
             "hosts": entry["hosts"], "allow": entry["allow"], "needs": entry["needs"],
             "requires": entry["requires"], "signed_by": entry.get("signed_by"), "added": entry.get("added"),
             "used_by": users_of(entry["sha256"], runs)}
            for name, versions in sorted(found.items())
            for version, entry in sorted(versions.items(), key=lambda item: version_key(item[0]))]


def catalog_listing(entries, found=None, arch=None):
    """The catalog as `toolchain catalog --json` shows it: the entries of this machine's architecture, newest
    version of a name first, with whether each is in the store and whether its catalog is signed."""
    found = installed() if found is None else found
    arch = arch or machine()
    rows = []
    for entry in sorted(entries, key=lambda e: (e["name"], [-n for n in version_key(e["version"])[0]])):
        if entry["arch"] != arch:
            continue
        have = found.get(entry["name"], {}).get(entry["version"])
        rows.append({"name": entry["name"], "version": entry["version"], "arch": entry["arch"],
                     "title": entry["title"], "description": entry["description"], "license": entry["license"],
                     "homepage": entry["homepage"], "size": entry["size"], "unpacked": entry["unpacked"],
                     "commands": sorted(entry["commands"]), "hosts": entry["hosts"], "allow": entry["allow"],
                     "needs": entry["needs"], "requires": entry["requires"],
                     "signed": (entry.get("_signature") or {}).get("state") == "good",
                     "installed": bool(have and have["sha256"] == entry["sha256"])})
    return rows


def table(rows):
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    return ["  ".join(cell.ljust(width) for cell, width in zip(row, widths)) + "  " + row[-1] for row in rows]


def size_shown(value):
    return f"{value / (1 << 20):.0f} MiB" if value else "-"


def needs_shown(needs):
    parts = ([f"glibc {needs['glibc']}+"] if needs.get("glibc") else []) + list(needs.get("libraries") or [])
    return ", ".join(parts) or "nothing but the kernel"


def show_lines(entry):
    lines = [f"{entry['name']} {entry['version']} ({entry['arch']})"
             + (f": {shown(entry['title'])}" if entry.get("title") else "")]
    if entry.get("description"):
        lines.append(f"  {shown(entry['description'], 1000)}")
    signer = entry.get("signed_by") or (entry.get("_signature") or {}).get("signer")
    unsigned = "nobody (added unsigned)" if "path" in entry else "nobody: its catalog is not signed by a trusted key"
    lines += [f"  licence: {shown(entry.get('license') or '-')}   {shown(entry.get('homepage') or '')}".rstrip(),
              f"  in the sandbox: {MOUNT}/{entry['name']} (read-only)",
              "  commands: " + ", ".join(f"{command} ({path})" for command, path in entry["commands"].items()),
              "  variables: " + (", ".join([*(f"{key}={expanded(value, entry['name'])}"
                                              for key, value in entry["env"].items()),
                                            *(f"{key}=~/{path}" for key, path in entry["caches"].items())])
                                 or "none"),
              "  opens: " + (", ".join([*entry["hosts"], *(f"--allow-{preset}" for preset in entry["allow"])])
                             or "nothing"),
              f"  needs: {needs_shown(entry['needs'])}"
              + (f"; the toolchains {', '.join(entry['requires'])}" if entry["requires"] else ""),
              f"  archive: {shown(entry['url'])}", f"  sha256: {entry['sha256']}",
              f"  signed by: {signer or unsigned}"]
    if "path" in entry:
        lines.append(f"  store: {entry['path']}")
    for source in entry.get("sources") or []:
        lines.append(f"  from: {shown(source['url'])} sha256 {source['sha256']}"
                     + (f" ({shown(source['signature'])})" if source.get("signature") else ""))
    return lines


# --- command line ---------------------------------------------------------------------------------------------


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec_ = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def parser():
    parser = argparse.ArgumentParser(prog="sandbox-ai toolchain", description=__doc__.split("\n\n")[0],
                                     formatter_class=help_style, epilog="\n\n".join(__doc__.split("\n\n")[2:5]))
    commands = parser.add_subparsers(dest="command", required=True)
    catalog = commands.add_parser("catalog", help="what the signed catalog offers for this machine")
    catalog.add_argument("--json", action="store_true", help="as JSON")
    catalog.add_argument("--catalog", metavar="FILE_OR_URL", help="another catalog (its signature is checked too)")
    adder = commands.add_parser("add", help="download a toolchain, check it, unpack it into the store")
    adder.add_argument("name", metavar="NAME[@VERSION]")
    adder.add_argument("--catalog", metavar="FILE_OR_URL", help="another catalog (its signature is checked too)")
    adder.add_argument("--unsigned", action="store_true",
                       help="take an entry of a catalog that is not signed by a trusted key (never one whose "
                            "signature is bad)")
    lister = commands.add_parser("list", help="the store; --run: a running sandbox's toolchains")
    lister.add_argument("--run", metavar="RUN", nargs="?", const="", help="the toolchains of a running sandbox")
    lister.add_argument("--json", action="store_true", help="as JSON")
    shower = commands.add_parser("show", help="a toolchain's manifest: commands, variables, hosts, needs, sources")
    shower.add_argument("name", metavar="NAME[@VERSION]")
    shower.add_argument("--json", action="store_true", help="as JSON")
    shower.add_argument("--catalog", metavar="FILE_OR_URL", help="another catalog (for one not in the store)")
    remover = commands.add_parser("remove", help="take a toolchain out of the store")
    remover.add_argument("name", metavar="NAME[@VERSION]")
    attacher = commands.add_parser("attach", help="mount a toolchain into a running sandbox, its commands on PATH")
    attacher.add_argument("args", nargs="+", metavar="[RUN] NAME[@VERSION]")
    attacher.add_argument("--offline", action="store_true", help="do not open its registries")
    detacher = commands.add_parser("detach", help="take a toolchain out of a running sandbox again")
    detacher.add_argument("args", nargs="+", metavar="[RUN] NAME")
    detacher.add_argument("--force", action="store_true",
                          help="also while a process of the sandbox runs from it or has a file of it open")
    return parser


def main(argv, out=print):
    args = parser().parse_args(argv)
    try:
        if args.command == "catalog":
            entries, signature, problems = read_catalog(args.catalog)
            rows = catalog_listing(entries)
            if args.json:
                out(json.dumps({"toolchains": rows, "signature": signature, "problems": problems,
                                "arch": machine()}, indent=2))
                return 0
            for problem in problems:
                print(f"sandbox-ai toolchain: {shown(problem, 500)}", file=sys.stderr)
            state = f"signed by {signature['signer']}" if signature["state"] == "good" else \
                f"NOT signed by a trusted key ({signature.get('reason')})"
            out(f"the catalog is {state}")
            if not rows:
                out(f"it has no toolchain for this machine ({machine()})")
                return 0
            out("\n".join(table([["NAME", "VERSION", "SIZE", "NEEDS", "IN STORE", "COMMANDS"], *(
                [row["name"], row["version"], size_shown(row["unpacked"] or row["size"]), needs_shown(row["needs"]),
                 "yes" if row["installed"] else "-", ", ".join(row["commands"][:8])
                 + (" ..." if len(row["commands"]) > 8 else "")] for row in rows)])))
            return 0
        if args.command == "add":
            add(args.name, args.catalog, args.unsigned, out=out)
            return 0
        if args.command == "remove":
            remove(args.name, out=out)
            return 0
        if args.command == "show":
            name, version = spec(args.name)
            try:
                entry = chosen(installed(), name, version)
            except ValueError:
                entry = find_entry(read_catalog(args.catalog)[0], name, version)
            out(json.dumps({key: value for key, value in entry.items() if not key.startswith("_")}, indent=2)
                if args.json else "\n".join(show_lines(entry)))
            return 0
        net = module("net")
        if args.command == "list" and args.run is None:
            rows = store_listing()
            if args.json:
                out(json.dumps(rows, indent=2))
            elif not rows:
                out("no toolchain is in the store (sandbox-ai toolchain catalog, then: sandbox-ai toolchain add NAME)")
            else:
                out("\n".join(table([["NAME", "VERSION", "SIZE", "SIGNED", "USED BY", "COMMANDS"], *(
                    [row["name"], row["version"], size_shown(row["size"]), "yes" if row["signed_by"] else "NO",
                     ", ".join(map(shown, row["used_by"])) or "-", ", ".join(row["commands"][:8])
                     + (" ..." if len(row["commands"]) > 8 else "")] for row in rows)])))
            return 0
        if args.command == "list":
            runs = net.running()
            if args.json and not args.run and not runs:
                out("[]")
                return 0
            record = net.choose(runs, args.run or None, command="toolchain list")
            rows = run_listing(record)
            if args.json:
                out(json.dumps(rows, indent=2))
            elif not rows:
                out(f"no toolchain is attached to {shown(record.get('run'))}")
            else:
                out("\n".join(table([["NAME", "VERSION", "GIVEN", "AT", "COMMANDS"], *(
                    [row["name"], row["version"], "at the start" if row["source"] == "start" else "during the run",
                     row["sandbox"], ", ".join(row["commands"][:8]) + (" ..." if len(row["commands"]) > 8 else "")]
                    for row in rows)])))
            return 0
        if len(args.args) > 2:
            raise ValueError(f"give [RUN] NAME{'[@VERSION]' if args.command == 'attach' else ''}")
        run, value = args.args if len(args.args) == 2 else (None, args.args[0])
        record = net.choose(net.running(), run, command=f"toolchain {args.command}")
        if args.command == "attach":
            attach(record, value, offline=args.offline, out=out)
        else:
            detach(record, value, force=args.force, out=out)
        return 0
    except (ValueError, OSError) as exc:
        print(f"sandbox-ai toolchain: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--internal-helper":
        sys.exit(helper_main(sys.argv[2]))
    sys.exit(main(sys.argv[1:]))
