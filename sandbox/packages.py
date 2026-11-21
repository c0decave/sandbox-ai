#!/usr/bin/env python3
"""sandbox-ai packages: the package guard's host side - the offline copy of the known malicious packages and the
packages you allow.

  sandbox-ai packages [status] [--json]              the offline copy: its age, its entries, who signed it; the guard
  sandbox-ai packages sync [--source URL | --from DIR] [--sha256 HEX] [--json]
                                                     fetch the signed copy and check it; replaces the one here
  sandbox-ai packages sync --osv [--from DIR]        build the copy here from OSV's own dumps (not signed: said)
  sandbox-ai packages check SPEC... [--json]         is this package on the list? Looked up here, nothing is sent
  sandbox-ai packages allow SPEC... [--project DIR]  allow a package in every run of the project
  sandbox-ai packages forget SPEC... [--project DIR] take that back
  sandbox-ai packages list [--project DIR | --all] [--json]     what is allowed
  sandbox-ai packages build --from DIR --out DIR [--sign KEY]   maintainers: make a copy to publish from OSV's dumps

SPEC is ECOSYSTEM:NAME or ECOSYSTEM:NAME@VERSION, the ecosystem one of npm, pypi, crates, go: npm:left-pad,
npm:@scope/name@1.2.3, pypi:requests@2.32.0, crates:serde, go:github.com/x/y@v1.2.3.

The guard itself is the connection proxy's (runtime.PackageGuard): for what npm, pip, cargo and go download, it
refuses a version that OSV lists as malicious (the advisory MAL-...), asks before a version younger than 14 days, and
records a name the registry does not know or that is brand new (asked about in the modes high and paranoid). Release
dates come from the registry's answers to the agent's own requests. The malicious packages are looked up in the copy
on this machine and nowhere else: no package name ever leaves it for a lookup.

The copy (~/.local/share/sandbox-ai/osv-malicious): osv-malicious.json (when it was made, each list's SHA-256) with
its signature osv-malicious.json.sig, and one list per ecosystem (npm.list ...: NAME, advisory, versions; sorted,
so the proxy searches the file in place). `sync` takes it from the release source (update_source, as sandbox-ai
update) under releases/download/osv-malicious, or from a folder; the signature must be a release key's (the keys
of sandbox/release-signers, in a namespace of its own), every list must match its checksum, and a copy older than
the one here is refused. A copy of your own making that no release key signed (a mirror in your network, made with
`packages build`) is taken only when you name its osv-malicious.json's SHA-256 yourself (--sha256); status and doctor
say that it is not signed. `sandbox-ai update` syncs it along. `sync --osv` instead downloads OSV's dumps of whole
ecosystems (osv-vulnerabilities.storage.googleapis.com/ECOSYSTEM/all.zip: every advisory, no package of yours is
named) and builds the lists here; nobody signs those, and status and doctor say so.

The allow-list (~/.config/sandbox-ai/packages.json, per project; --allow-package for one start) lets a package pass
the age check and the new-name check. It never covers a version on the malicious list: that one is allowed by your
answer to its question only, for that one download.

Standard library only.
"""
import argparse
import contextlib
import fcntl
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile

HERE = Path(__file__).resolve().parent
LOADING = threading.RLock()
_modules = {}

FORMAT = 1
MANIFEST, SIGNATURE = "osv-malicious.json", "osv-malicious.json.sig"
STATE = "state.json"                      # beside the copy: when it was synced, from where, who signed it
RELEASE_TAG = "osv-malicious"             # SOURCE/releases/download/osv-malicious/FILE
OSV_DUMPS = "https://osv-vulnerabilities.storage.googleapis.com"
OSV_NAMES = {"npm": "npm", "pypi": "PyPI", "crates": "crates.io", "go": "Go"}  # ours: OSV's ecosystem (and folder)
ECOSYSTEMS = tuple(OSV_NAMES)             # runtime.PACKAGE_ECOSYSTEMS (a test keeps them the same)
MANIFEST_LIMIT = 1 << 20
LIST_LIMIT = 512 << 20                    # bytes of one list, unpacked
DUMP_LIMIT = 4 << 30                      # bytes of one of OSV's dumps
ENTRY_LIMIT = 8 << 20                     # bytes of one advisory in a dump
VERSIONS_LIMIT = 500                      # versions listed for one advisory; more: every version
STALE_DAYS = 14                           # doctor and status warn about a copy older than this
SYNC_EVERY = 86400                        # `sandbox-ai update` syncs at most this often by itself
FETCH_TIMEOUT = 60
UPDATE_TIMEOUT = 15                       # the sync that goes along with an update never holds it up for long


def module(name):
    """A sibling module, loaded once by its path (the launcher runs with python -I)."""
    with LOADING:
        if name not in _modules:
            spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_packages", HERE / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            _modules[name] = loaded
        return _modules[name]


def runtime():
    return module("runtime")


def osv_ring():
    """The key ring that signs the copy: the release keys, in a namespace of its own - a release's signature is
    none of a copy's, and the other way round."""
    keys = module("release_keys")
    return {"namespace": "sandbox-ai-osv", "principal": keys.PRINCIPAL, "signers": keys.SIGNERS, "label": "release"}


def printable(text, limit=300):
    text = "".join(char if char.isprintable() else "?" for char in str(text))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def stamp(seconds=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def stamp_seconds(value):
    """The seconds since the epoch of a time as stamp() writes it (2026-11-19T10:00:00Z), or None. The copy's own
    times: read without the runtime (doctor, the status and a start's note load no 18000 lines for a date)."""
    import calendar
    try:
        return calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")) if isinstance(value, str) else None
    except (ValueError, OverflowError):
        return None


# --- where things are ---------------------------------------------------------------------------------------------

def data_dir():
    """The offline copy's folder."""
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "sandbox-ai" / "osv-malicious"


def allow_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "packages.json"


# --- the lists ----------------------------------------------------------------------------------------------------

def list_line(name, advisory, versions):
    """One line of a list: NAME, ADVISORY and VERSIONS (None: every version)."""
    return f"{name}\t{advisory}\t{'*' if versions is None else ','.join(sorted(versions))}\n".encode("ascii")


def check_list(data, name="the list"):
    """The number of entries of the list DATA (bytes); ValueError when it is not one: every line NAME, advisory and
    versions, in ASCII, and the lines sorted - the proxy searches it as a sorted file, and one that is not would
    hide entries."""
    rt = runtime()
    count, before = 0, b""
    for number, line in enumerate(data.split(b"\n"), 1):
        if not line:
            if number <= data.count(b"\n"):
                raise ValueError(f"{name}, line {number}: an empty line")
            continue
        fields = line.split(b"\t")
        try:
            text = [field.decode("ascii") for field in fields]
        except UnicodeDecodeError:
            raise ValueError(f"{name}, line {number}: not ASCII") from None
        if len(text) != 3 or not rt.PACKAGE_NAME.fullmatch(text[0]) or not rt.PACKAGE_ADVISORY.fullmatch(text[1]) or \
                not (text[2] == "*" or all(rt.PACKAGE_VERSION.fullmatch(v) for v in text[2].split(","))):
            raise ValueError(f"{name}, line {number}: not NAME, advisory and versions")
        if line <= before:
            raise ValueError(f"{name}, line {number}: not sorted (or twice)")
        before, count = line, count + 1
    if data and not data.endswith(b"\n"):
        raise ValueError(f"{name}: the last line is cut")
    return count


def affected_versions(affected, ecosystem):
    """The versions an OSV "affected" entry names for the lists: None for every version - a range from the first
    release on with no fix, no version said at all, or more than the lists hold - else the versions it lists. A
    range with bounds but no list of versions counts as every version: the lists compare no version order, and too
    much refused (you may still allow the download) is the safe side."""
    rt = runtime()
    ranges = affected.get("ranges") if isinstance(affected.get("ranges"), list) else []
    for entry in ranges:
        events = entry.get("events") if isinstance(entry, dict) and isinstance(entry.get("events"), list) else []
        keys = {key for event in events if isinstance(event, dict) for key in event}
        if "introduced" in keys and not keys & {"fixed", "last_affected", "limit"}:
            return None
    listed = affected.get("versions") if isinstance(affected.get("versions"), list) else []
    versions = {rt.package_version(ecosystem, value) for value in listed}
    versions.discard(None)
    return versions if versions and len(versions) <= VERSIONS_LIMIT and len(versions) == len(set(map(str, listed))) \
        else None


def advisory_lines(document):
    """{ecosystem: [(name, advisory, versions)]} of one OSV advisory DOCUMENT; {} unless it is a malicious-package
    one (MAL-...) that was not withdrawn."""
    rt = runtime()
    ident = document.get("id") if isinstance(document, dict) else None
    if not isinstance(ident, str) or not ident.startswith("MAL-") or not rt.PACKAGE_ADVISORY.fullmatch(ident) or \
            document.get("withdrawn"):
        return {}
    ours = {theirs: mine for mine, theirs in OSV_NAMES.items()}
    found = {}
    for affected in document.get("affected") if isinstance(document.get("affected"), list) else []:
        package = affected.get("package") if isinstance(affected, dict) else None
        if not isinstance(package, dict):
            continue
        ecosystem = ours.get(str(package.get("ecosystem", "")).split(":")[0])
        name = rt.package_name(ecosystem, package.get("name")) if ecosystem else None
        if name:
            found.setdefault(ecosystem, []).append((name, ident, affected_versions(affected, ecosystem)))
    return found


def merge_lines(entries):
    """The list (bytes) of ENTRIES [(name, advisory, versions)]: one line per name and advisory (its versions joined;
    every version wins), sorted by their bytes."""
    merged = {}
    for name, advisory, versions in entries:
        key = (name, advisory)
        if versions is None or merged.get(key, ()) is None:
            merged[key] = None
        else:
            merged[key] = set(versions) | merged.get(key, set())
    return b"".join(sorted(list_line(name, advisory, versions) for (name, advisory), versions in merged.items()))


def read_dump(path, into):
    """Add the malicious-package advisories of the OSV dump PATH (a .zip of advisories, a folder of .json files, or
    one .json) to INTO ({ecosystem: [entries]}); returns how many advisories it read."""
    count = 0

    def take(data):
        nonlocal count
        with contextlib.suppress(ValueError, RecursionError, UnicodeError):
            for ecosystem, entries in advisory_lines(json.loads(data)).items():
                into.setdefault(ecosystem, []).extend(entries)
        count += 1
    path = Path(path)
    if path.is_dir():
        for file in sorted(path.rglob("*.json")):
            if file.is_file() and file.stat().st_size <= ENTRY_LIMIT:
                take(file.read_bytes())
        for file in sorted(path.rglob("*.zip")):
            count += read_dump(file, into)
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                # Only the malicious-package advisories are opened (a dump holds every advisory of the ecosystem).
                name = info.filename.rsplit("/", 1)[-1]
                if name.startswith("MAL-") and name.endswith(".json") and info.file_size <= ENTRY_LIMIT:
                    take(archive.read(info))
    elif path.is_file() and path.stat().st_size <= ENTRY_LIMIT:
        take(path.read_bytes())
    else:
        raise ValueError(f"{path}: not a folder, a .zip or a .json of OSV advisories")
    return count


def build(sources, generated=None, source="OSV (osv.dev)"):
    """(manifest, {file name: list bytes}) of a copy made from the OSV dumps SOURCES (read_dump)."""
    rt = runtime()
    entries = {}
    read = sum(read_dump(path, entries) for path in sources)
    if not read:
        raise ValueError("no OSV advisory found in " + ", ".join(str(path) for path in sources))
    files, listed = {}, {}
    for ecosystem in rt.PACKAGE_ECOSYSTEMS:
        data = merge_lines(entries.get(ecosystem, []))
        files[f"{ecosystem}.list"] = data
        listed[f"{ecosystem}.list"] = {"sha256": hashlib.sha256(data).hexdigest(), "entries": check_list(data),
                                       "bytes": len(data)}
    return {"format": FORMAT, "generated": generated or stamp(), "source": source, "files": listed}, files


def manifest_bytes(manifest):
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def write_published(directory, manifest, files, key=None):
    """The copy as it is published, in DIRECTORY: osv-malicious.json, its signature by KEY (a private key file or the
    .pub of one in ssh-agent; None: unsigned - sync refuses that) and osv-malicious-ECOSYSTEM.list.gz each."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data = manifest_bytes(manifest)
    (directory / MANIFEST).write_bytes(data)
    if key:
        (directory / SIGNATURE).write_bytes(module("release_keys").sign(data, key, osv_ring()))
    for name, content in files.items():
        with open(directory / f"osv-malicious-{name}.gz", "wb") as stream, \
                gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as packed:
            packed.write(content)
    return directory


# --- fetching -----------------------------------------------------------------------------------------------------

class NotPublished(ValueError):
    """The source has no copy at all (no osv-malicious.json there): nothing to check, nothing refused."""


def check_url(url):
    """URL as a source: https, or http to this machine itself (a mirror of your own, the tests)."""
    parsed = urllib.parse.urlsplit(url)
    local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    if parsed.scheme not in ("https", "http") or not parsed.hostname or (parsed.scheme == "http" and not local) or \
            parsed.username or parsed.password:
        raise ValueError(f"{printable(url)}: the source is an https:// address (without a user or password in it) "
                         "or a folder")
    return url.rstrip("/")


def fetch(url, limit, into=None, timeout=None):
    """The bytes at URL (at most LIMIT; ValueError beyond), or - INTO, a file open for writing - written there."""
    import urllib.error
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": "sandbox-ai-packages", "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout or FETCH_TIMEOUT) as answer:  # noqa: S310 - check_url
            chunks, size = [], 0
            while True:
                chunk = answer.read(1 << 16)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ValueError(f"{printable(url)}: larger than {limit >> 20} MiB")
                (into.write if into is not None else chunks.append)(chunk)
            return b"".join(chunks)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotPublished(f"{printable(url)}: not there (HTTP 404)") from None
        raise ValueError(f"{printable(url)}: HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise ValueError(f"{printable(url)}: {printable(getattr(exc, 'reason', None) or exc)}") from None


def source_reader(source, timeout=None):
    """(read(name, limit) -> bytes, where) for a published copy at SOURCE: a folder or an https:// address."""
    if "://" in str(source):
        base = check_url(str(source))
        return (lambda name, limit: fetch(f"{base}/{name}", limit, timeout=timeout)), base
    folder = Path(source).expanduser()
    if not folder.is_dir():
        raise ValueError(f"{folder}: no such folder")

    def read(name, limit):
        path = folder / name
        try:
            if path.stat().st_size > limit:
                raise ValueError(f"{path}: larger than {limit >> 20} MiB")
            return path.read_bytes()
        except FileNotFoundError:
            raise NotPublished(f"{path}: not there") from None
        except OSError as exc:
            raise ValueError(f"{path}: {exc.strerror or exc}") from None
    return read, str(folder)


def default_source(base=None):
    """Where the signed copy is published: the release source BASE (default: update_source in defaults.json, else
    SANDBOX_AI_SOURCE, else the project's own - as sandbox-ai update), under releases/download/osv-malicious."""
    updates = module("updates")
    if base is None:
        with contextlib.suppress(Exception):
            base = updates.settings()["source"]
    return f"{updates.release_source(base).rstrip('/')}/releases/download/{RELEASE_TAG}"


def signatures_required():
    """The administrator's policy demands signatures (require_signed_updates in /etc/sandbox-ai/policy.json): a copy
    no release key signed is then never taken - not one built here from OSV's dumps, not one by its checksum. A
    policy that cannot be read counts as demanding them."""
    try:
        return bool(module("agent_sandbox").load_policy().get("require_signed_updates", False))
    except (Exception, SystemExit):  # noqa: BLE001 - an unreadable policy stops (fail closed): the strict reading
        return True


# --- the copy on this machine ---------------------------------------------------------------------------------------

def read_json(path, limit=MANIFEST_LIMIT):
    try:
        data = json.loads(Path(path).read_bytes()[:limit])
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def checked_manifest(data, where):
    """The manifest in DATA (bytes) as a dict; ValueError when it is not one of this format."""
    try:
        manifest = json.loads(data)
    except (ValueError, RecursionError, UnicodeError):
        manifest = None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or \
            stamp_seconds(manifest.get("generated")) is None or not isinstance(files, dict) or \
            set(files) - {f"{ecosystem}.list" for ecosystem in ECOSYSTEMS} or not files or \
            any(not isinstance(entry, dict) or not isinstance(entry.get("sha256"), str) or
                len(entry["sha256"]) != 64 for entry in files.values()):
        raise ValueError(f"{where}/{MANIFEST}: not a copy of the malicious packages in format {FORMAT}")
    return manifest


def status(directory=None, now=None):
    """What `packages status` and doctor say of the copy in DIRECTORY (data_dir): {"present", "directory",
    "generated", "age_days", "stale", "signed", "signer", "synced", "source", "entries": {ecosystem: n}, "total",
    "problem"}. Reads the manifest and the state only - never the lists (they may be large)."""
    directory = Path(directory) if directory is not None else data_dir()
    result = {"present": False, "directory": str(directory), "generated": None, "age_days": None, "stale": False,
              "signed": False, "signer": None, "unsigned": None, "synced": None, "source": None, "entries": {},
              "total": 0,
              "problem": None}
    manifest_path = directory / MANIFEST
    if not manifest_path.is_file():
        return result
    try:
        manifest = checked_manifest(manifest_path.read_bytes()[:MANIFEST_LIMIT], str(directory))
    except (OSError, ValueError) as exc:
        return {**result, "problem": printable(exc)}
    state = read_json(directory / STATE) or {}
    made = stamp_seconds(manifest["generated"])
    age = max(0, int(((time.time() if now is None else now) - made) // 86400))
    entries = {name[:-5]: entry.get("entries") if isinstance(entry.get("entries"), int) else 0
               for name, entry in manifest["files"].items()}
    missing = [name for name in manifest["files"] if not (directory / name).is_file()]
    return {**result, "present": True, "generated": manifest["generated"], "age_days": age,
            "stale": age > STALE_DAYS, "signed": state.get("signed") is True,
            "signer": state.get("signer") if isinstance(state.get("signer"), str) else None,
            # why a copy without a release key's signature was taken: you named its checksum, or it was built here
            "unsigned": None if state.get("signed") is True else
            "taken by the SHA-256 you named" if isinstance(state.get("checksum"), str) else
            "built here from OSV's dumps",
            "synced": state.get("synced") if isinstance(state.get("synced"), str) else None,
            "source": printable(state.get("source") or manifest.get("source") or "", 300) or None,
            "entries": entries, "total": sum(entries.values()),
            "problem": f"missing: {', '.join(missing)}" if missing else None}


def mount_dir(directory=None):
    """The folder a start mounts for its proxy (runtime.PACKAGES_DIR), or None when there is no copy to mount."""
    directory = Path(directory) if directory is not None else data_dir()
    return directory if (directory / MANIFEST).is_file() else None


def install(directory, manifest, files, state):
    """Put the copy (MANIFEST, FILES {name: bytes}, its STATE) in DIRECTORY, whole or not at all: written beside it
    and swapped in. A proxy that runs keeps the lists it mapped."""
    directory = Path(directory)
    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fresh = Path(tempfile.mkdtemp(prefix=".osv-malicious-", dir=directory.parent))
    try:
        for name, data in files.items():
            (fresh / name).write_bytes(data)
        (fresh / MANIFEST).write_bytes(manifest_bytes(manifest))
        if state.get("signature"):
            (fresh / SIGNATURE).write_bytes(state.pop("signature"))
        (fresh / STATE).write_text(json.dumps(state, indent=2) + "\n")
        for path in fresh.iterdir():  # the proxy of a sandbox reads them as another user: world-readable, not secret
            path.chmod(0o644)
        fresh.chmod(0o755)
        old = directory.with_name(f".{directory.name}.old-{os.getpid()}")
        if directory.exists():
            os.rename(directory, old)
        try:
            os.rename(fresh, directory)
        except OSError:
            if old.exists():
                os.rename(old, directory)
            raise
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(fresh, ignore_errors=True)


def sync(source=None, directory=None, keys=None, out=print, timeout=None, checksum=None):
    """Fetch the published copy from SOURCE (a folder or an https:// address; default_source()), check it and put it
    in DIRECTORY: its manifest must be signed by one of KEYS (the release keys) - or, CHECKSUM given (the SHA-256 of
    its osv-malicious.json, which the human names: a copy of their own making), have exactly that checksum - and must
    not be older than the copy here, every list must match the manifest's SHA-256 and be a sorted list. Returns the
    new status; ValueError says what was refused - the copy here then stays as it was."""
    release_keys = module("release_keys")
    directory = Path(directory) if directory is not None else data_dir()
    read, where = source_reader(source if source is not None else default_source(), timeout)
    try:
        data = read(MANIFEST, MANIFEST_LIMIT)
    except NotPublished as exc:
        raise NotPublished(f"no copy of the known malicious packages is published at {where} ({exc}). "
                           "sandbox-ai packages sync --osv builds one here from OSV's own dumps") from None
    signature, detail = None, None
    if checksum is not None:
        wanted = str(checksum).strip().lower()
        if len(wanted) != 64 or set(wanted) - set("0123456789abcdef"):
            raise ValueError("--sha256 takes the SHA-256 of the copy's osv-malicious.json (64 hex digits)")
        if hashlib.sha256(data).hexdigest() != wanted:
            raise ValueError(f"{where}: {MANIFEST} does not have the SHA-256 you named - not taken")
    else:
        try:
            signature = read(SIGNATURE, MANIFEST_LIMIT)
        except ValueError as exc:
            raise ValueError(f"{where}: no signature ({exc}) - an unsigned copy is never taken (a copy of your own "
                             f"making: name its {MANIFEST}'s SHA-256 with --sha256)") from None
        keys = release_keys.load() if keys is None else keys
        if not keys:
            raise ValueError("this installation knows no release key (sandbox/release-signers): nothing could prove a "
                             "copy")
        verdict, detail = release_keys.verify(data, signature, keys, osv_ring(), what=MANIFEST)
        if verdict != "good":
            raise ValueError(f"{where}: {detail} - not taken")
    manifest = checked_manifest(data, where)
    have = status(directory)
    if have["present"] and not have["problem"] and \
            stamp_seconds(manifest["generated"]) < stamp_seconds(have["generated"]):
        raise ValueError(f"{where}: its copy is older ({manifest['generated']}) than the one here "
                         f"({have['generated']}) - not taken")
    files = {}
    for name, entry in sorted(manifest["files"].items()):
        packed = read(f"osv-malicious-{name}.gz", LIST_LIMIT)
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:
                content = stream.read(LIST_LIMIT + 1)
        except (OSError, EOFError, ValueError):
            raise ValueError(f"{where}: osv-malicious-{name}.gz is not a gzip file - not taken") from None
        if len(content) > LIST_LIMIT or hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise ValueError(f"{where}: osv-malicious-{name}.gz does not match its checksum in the signed "
                             f"{MANIFEST} - not taken")
        check_list(content, name)
        files[name] = content
        out(f"{name}: {content.count(bytes([10]))} entries")
    install(directory, manifest, files, {"synced": stamp(), "source": where, "signed": signature is not None,
                                         "signer": detail, **({"signature": signature} if signature else {}),
                                         **({"checksum": hashlib.sha256(data).hexdigest()} if checksum else {})})
    return status(directory)


def sync_osv(sources=None, directory=None, out=print, download=fetch):
    """Build the copy in DIRECTORY from OSV's own dumps: SOURCES (folders, .zip files) or, without, each ecosystem's
    dump downloaded whole from OSV_DUMPS. Nobody signed those: the state says so."""
    directory = Path(directory) if directory is not None else data_dir()
    with contextlib.ExitStack() as stack:
        if not sources:
            directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=".osv-dumps-", dir=directory.parent)))
            sources = []
            for ours, theirs in OSV_NAMES.items():
                url = f"{OSV_DUMPS}/{urllib.parse.quote(theirs)}/all.zip"
                out(f"fetching {url} (every advisory of {theirs}: no package of yours is named)")
                with open(scratch / f"{ours}.zip", "wb") as stream:
                    download(url, DUMP_LIMIT, stream)
                sources.append(scratch / f"{ours}.zip")
            where = f"{OSV_DUMPS} (OSV's dumps, built here)"
        else:
            where = "OSV dumps in " + ", ".join(str(path) for path in sources) + " (built here)"
        manifest, files = build(sources, source="OSV (osv.dev), built on this machine")
    for name, entry in manifest["files"].items():
        out(f"{name}: {entry['entries']} entries")
    install(directory, manifest, files, {"synced": stamp(), "source": where, "signed": False, "signer": None})
    return status(directory)


def sync_due(directory=None, now=None):
    """`sandbox-ai update` syncs along: not when the copy here was synced within SYNC_EVERY."""
    state = read_json((Path(directory) if directory is not None else data_dir()) / STATE) or {}
    synced = stamp_seconds(state.get("synced"))
    return synced is None or (time.time() if now is None else now) - synced >= SYNC_EVERY


def sync_with_update(out=print, directory=None, source=None, base=None):
    """What `sandbox-ai update` does for the copy: the signed sync from the release source (BASE: the one the update
    uses), at most once a day. A source that publishes no copy is passed over without a word (doctor says that there
    is none here); any other failure - a copy that is not signed, older, damaged - is said in one line and fails
    nothing: the update itself went through. Returns the new status, or None when nothing was taken."""
    if not sync_due(directory):
        return None
    try:
        result = sync(source if source is not None else default_source(base), directory, out=lambda line: None,
                      timeout=UPDATE_TIMEOUT)
    except NotPublished:
        return None
    except (ValueError, OSError, RuntimeError) as exc:
        out(f"sandbox-ai: the copy of the known malicious packages was not synced: {printable(exc, 400)} "
            "(sandbox-ai packages sync tries again; sandbox-ai packages status shows the copy here)")
        return None
    out(f"sandbox-ai: synced the known malicious packages: {result['total']} entries, made {result['generated']}")
    return result


def lookup(spec, directory=None, whole=False):
    """{"spec", "ecosystem", "name", "version", "malicious": [advisory ...]} for SPEC, looked up in the copy here. A
    SPEC without a version names every advisory of the package - or, WHOLE, only those for the whole package (what
    an allow-list entry for the name would have to cover)."""
    rt = runtime()
    ecosystem, name, version = rt.package_spec(spec)
    directory = Path(directory) if directory is not None else data_dir()
    found = rt.PackageList.open(directory / f"{ecosystem}.list").find(name)
    return {"spec": f"{ecosystem}:{name}" + (f"@{version}" if version else ""), "ecosystem": ecosystem, "name": name,
            "version": version,
            "malicious": [advisory for advisory, versions in found
                          if versions is None or (version is None and not whole) or (version and version in versions)]}


# --- the allow-list -------------------------------------------------------------------------------------------------

def spec_text(spec):
    ecosystem, name, version = runtime().package_spec(spec)
    return f"{ecosystem}:{name}" + (f"@{version}" if version else "")


def project_key(project):
    return str(Path(project).expanduser().resolve())


def read_allowed(path=None):
    """{project: [spec ...]} of the allow-list file (valid entries only); {} when there is none or it is damaged."""
    data = read_json(path or allow_file(), 4 << 20) or {}
    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    result = {}
    for project, entry in projects.items():
        specs = []
        for spec in entry.get("allow", []) if isinstance(entry, dict) and isinstance(entry.get("allow"), list) else []:
            with contextlib.suppress(ValueError):
                specs.append(spec_text(spec))
        if specs:
            result[str(project)] = list(dict.fromkeys(specs))
    return result


def allowed_for(project, path=None):
    """The packages allowed in every run of PROJECT (its folder)."""
    return read_allowed(path).get(project_key(project), [])


@contextlib.contextmanager
def allow_store(path=None):
    """The allow-list file as a dict, read and written back under a lock. A damaged file is kept aside, never
    written over."""
    path = Path(path or allow_file())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path.with_name(".packages.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = None
        if path.exists():
            data = read_json(path, 4 << 20)
            if data is None and path.read_bytes().strip():
                aside = path.with_name(f"{path.name}.damaged-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}")
                os.rename(path, aside)
                print(f"sandbox-ai: warning: {path} could not be read (not a JSON object): kept as {aside.name}; a "
                      "new one is begun", file=sys.stderr)
        data = data if isinstance(data, dict) else {}
        data.setdefault("version", 1)
        if not isinstance(data.get("projects"), dict):
            data["projects"] = {}
        yield data
        temporary = path.with_name(f".packages.{os.getpid()}.json")
        temporary.write_text(json.dumps(data, indent=2) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)


def allow(project, specs, path=None, directory=None):
    """Allow SPECS in every run of PROJECT; returns (added, refused): REFUSED [(spec, why)] are those on the
    malicious list here - an allow-list never covers them."""
    wanted = [spec_text(spec) for spec in specs]
    refused = [(spec, ", ".join(found["malicious"])) for spec in wanted
               for found in [lookup(spec, directory, whole=True)] if found["malicious"]]
    taken = [spec for spec in wanted if spec not in {spec for spec, _ in refused}]
    added = []
    if taken:
        with allow_store(path) as data:
            entry = data["projects"].setdefault(project_key(project), {})
            listed = entry.get("allow") if isinstance(entry.get("allow"), list) else []
            details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
            for spec in taken:
                if spec not in listed:
                    listed.append(spec)
                    details[spec] = {"added": stamp()}
                    added.append(spec)
            entry["allow"], entry["details"] = listed, details
    return added, refused


def forget(project, specs, path=None):
    """Take SPECS off PROJECT's allow-list; returns those that were on it."""
    wanted = [spec_text(spec) for spec in specs]
    gone = []
    with allow_store(path) as data:
        key = project_key(project)
        entry = data["projects"].get(key)
        if isinstance(entry, dict) and isinstance(entry.get("allow"), list):
            gone = [spec for spec in wanted if spec in entry["allow"]]
            entry["allow"] = [spec for spec in entry["allow"] if spec not in wanted]
            if isinstance(entry.get("details"), dict):
                for spec in gone:
                    entry["details"].pop(spec, None)
            if not entry["allow"]:
                data["projects"].pop(key, None)
    return gone


# --- what a start gives its proxy -------------------------------------------------------------------------------------

def proxy_settings(rulesets, mode=None, mode_name=None, allow=(), min_age_days=None, mode_base=None):
    """proxy.json's "packages" for a run: the defaults - MODE_BASE in place of them where the security mode has its
    own (lax records young versions instead of asking), MIN_AGE_DAYS (package_min_age_days of your defaults.json; 0:
    no age check) in place of the 14 days when given - with the mode's settings (MODE: modes.py's "packages") and
    every enforcing ruleset's "packages" section weighed in (runtime.package_decisions: the strictest wins), the
    audit rulesets' sections beside them ("audit": recorded, never enforced), and ALLOW (the run's and the project's
    allow-list entries)."""
    rt = runtime()
    base = dict(rt.PACKAGE_DEFAULTS)
    if isinstance(mode_base, dict):
        with contextlib.suppress(ValueError):
            base.update(rt.package_settings(mode_base))
    if isinstance(min_age_days, int) and not isinstance(min_age_days, bool) and \
            rt.PACKAGE_AGE_RANGE[0] <= min_age_days <= rt.PACKAGE_AGE_RANGE[1]:
        base["min_age_days"] = min_age_days
    sections = [(f"mode {mode_name}", mode)] if mode else []
    audit = []
    for source in rulesets:
        section = source.get("packages") if isinstance(source, dict) else None
        if not isinstance(section, dict):
            continue
        if source.get("mode") == "audit":
            audit.append({"name": source.get("name", "rules"), **section})
        else:
            sections.append((source.get("name", "rules"), section))
    settings = rt.package_decisions(sections, base)
    return {**settings, "allow": list(dict.fromkeys(allow)), **({"audit": audit} if audit else {})}


# --- the command ------------------------------------------------------------------------------------------------------

def status_lines(info, colour=None):
    """`packages status` as lines."""
    t = module("textout")
    if not info["present"]:
        return [t.style("no copy of the known malicious packages on this machine", "warn", colour) +
                ": the guard checks release age and new names only.", "Fetch it: sandbox-ai packages sync"]
    signed = f"signed by a release key ({info['signer']})" if info["signed"] else \
        t.style("not signed", "warn", colour) + f" ({info['unsigned']})"
    age = f"{info['age_days']} day{'' if info['age_days'] == 1 else 's'} old"
    lines = [f"The known malicious packages (OSV MAL-...): made {info['generated']}, "
             + (t.style(age, "warn", colour) if info["stale"] else age) + f", {signed}",
             f"  entries: {info['total']} (" + ", ".join(f"{name} {count}" for name, count in
                                                         sorted(info["entries"].items())) + ")",
             f"  synced:  {info['synced'] or '?'} from {info['source'] or '?'}", f"  folder:  {info['directory']}"]
    if info["problem"]:
        lines.append(t.style(f"  problem: {info['problem']}", "bad", colour) + " - sandbox-ai packages sync")
    elif info["stale"]:
        lines.append(f"  older than {STALE_DAYS} days: sandbox-ai packages sync (new malware is missing; the age "
                     "check covers part of the gap)")
    return lines


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai packages", description=__doc__.split("\n\n")[0],
                                epilog="SPEC: ECOSYSTEM:NAME[@VERSION] (npm, pypi, crates, go). Nothing is ever "
                                       "looked up outside this machine.")
    sub = p.add_subparsers(dest="command")
    shown = sub.add_parser("status", help="The offline copy: its age, entries, signature")
    shown.add_argument("--json", action="store_true", help="As JSON")
    fetched = sub.add_parser("sync", help="Fetch the signed copy of the known malicious packages and check it")
    fetched.add_argument("--source", metavar="URL", help="Where the copy is published (default: the release source, "
                                                         "under releases/download/osv-malicious)")
    fetched.add_argument("--from", dest="origin", metavar="DIR", action="append",
                         help="A folder with the published copy; with --osv: OSV dumps (folders, .zip files)")
    fetched.add_argument("--sha256", metavar="HEX",
                         help="Take a copy no release key signed - one of your own making - whose osv-malicious.json "
                              "has this SHA-256")
    fetched.add_argument("--osv", action="store_true",
                         help="Build the copy here from OSV's own dumps (whole ecosystems; not signed)")
    fetched.add_argument("--json", action="store_true", help="The new status as JSON")
    checked = sub.add_parser("check", help="Is a package on the list? Looked up here, nothing is sent")
    checked.add_argument("spec", nargs="+", metavar="SPEC")
    checked.add_argument("--json", action="store_true", help="As JSON")
    for name, text in (("allow", "Allow packages in every run of the project (never a known malicious version)"),
                       ("forget", "Take packages off the project's allow-list")):
        command = sub.add_parser(name, help=text)
        command.add_argument("spec", nargs="+", metavar="SPEC")
        command.add_argument("--project", metavar="DIR", default=".",
                             help="The project's folder: the first workspace of its starts (default: this one)")
    listed = sub.add_parser("list", help="The packages allowed for the project")
    listed.add_argument("--project", metavar="DIR", default=".",
                        help="The project's folder: the first workspace of its starts (default: this one)")
    listed.add_argument("-a", "--all", action="store_true", help="Every project's")
    listed.add_argument("--json", action="store_true", help="As JSON")
    made = sub.add_parser("build", help="Maintainers: make a copy to publish from OSV's dumps")
    made.add_argument("--from", dest="origin", metavar="DIR", action="append", required=True,
                      help="OSV dumps: folders of advisories or .zip files")
    made.add_argument("--out", metavar="DIR", required=True, help="Where the files to publish are written")
    made.add_argument("--sign", metavar="KEY", help="Sign with this release key (a private key file, or the .pub of "
                                                    "one in ssh-agent)")
    return p


def main(argv=None, out=print):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--json"]:
        argv = ["status", "--json"]
    a = parser().parse_args(argv or ["status"])
    try:
        if a.command == "status":
            info = status()
            for line in [json.dumps(info, indent=2)] if a.json else status_lines(info):
                out(line)
            return 0
        if a.command == "sync":
            if a.osv and (a.source or a.sha256):
                raise ValueError("--osv builds the copy from OSV's dumps: not with --source or --sha256")
            if not a.osv and a.origin and (a.source or len(a.origin) > 1):
                raise ValueError("one source: --source URL or --from DIR")
            if (a.osv or a.sha256) and signatures_required():
                raise ValueError("the administrator's policy requires signatures (require_signed_updates): only a copy "
                                 "signed by a release key is taken, not one built from OSV's dumps (--osv) or taken by "
                                 "its checksum (--sha256)")
            say = (lambda line: None) if a.json else out
            if a.osv:
                say("Building the copy from OSV's dumps: nobody signed them (TLS to their host is all that vouches "
                    "for them); sandbox-ai packages status and doctor will say so.")
                info = sync_osv(a.origin, out=say)
            else:
                info = sync(a.origin[0] if a.origin else a.source, out=say, checksum=a.sha256)
            for line in [json.dumps(info, indent=2)] if a.json else status_lines(info):
                out(line)
            return 0
        if a.command == "check":
            found = [lookup(spec) for spec in a.spec]
            if a.json:
                out(json.dumps(found, indent=2))
            else:
                if not status()["present"]:
                    out("no copy of the known malicious packages on this machine: sandbox-ai packages sync")
                for entry in found:
                    out(f"{printable(entry['spec'])}: " + ("KNOWN MALICIOUS: " + ", ".join(
                        f"{advisory} (https://osv.dev/vulnerability/{advisory})" for advisory in entry["malicious"])
                        if entry["malicious"] else "not on the list"))
            return 1 if any(entry["malicious"] for entry in found) else 0
        if a.command == "allow":
            added, refused = allow(a.project, a.spec)
            for spec in added:
                out(f"allowed {spec} in every run of {project_key(a.project)} (its age and a new name are not asked "
                    "about; from the next start on)")
            for spec in sorted(set(spec_text(s) for s in a.spec) - set(added) - {spec for spec, _ in refused}):
                out(f"{spec} was allowed already")
            for spec, advisories in refused:
                print(f"sandbox-ai packages: {spec} is on the list of known malicious packages ({advisories}): an "
                      "allow-list never covers it. Only your answer to its question allows one download.",
                      file=sys.stderr)
            return 1 if refused else 0
        if a.command == "forget":
            gone = forget(a.project, a.spec)
            for spec in gone:
                out(f"forgot {spec}")
            missing = sorted(set(spec_text(s) for s in a.spec) - set(gone))
            for spec in missing:
                print(f"sandbox-ai packages: {spec} is not on the allow-list of {project_key(a.project)}",
                      file=sys.stderr)
            return 1 if missing else 0
        if a.command == "list":
            known = read_allowed()
            if not a.all:
                key = project_key(a.project)
                known = {key: known[key]} if key in known else {}
            if a.json:
                out(json.dumps(known, indent=2))
            elif not known:
                out("(none)" + ("" if a.all else f" for {project_key(a.project)}; --all: every project's"))
            else:
                module("textout").table(["PROJECT", "PACKAGE"], [[printable(project), spec] for project, specs in
                                                                 sorted(known.items()) for spec in specs], out, indent=0)
            return 0
        if a.command == "build":
            manifest, files = build(a.origin)
            write_published(a.out, manifest, files, a.sign)
            for name, entry in manifest["files"].items():
                out(f"{name}: {entry['entries']} entries")
            out(f"written to {a.out}: {MANIFEST}" + (f", {SIGNATURE}" if a.sign else " (NOT signed: sync refuses it)")
                + f" and the lists; publish them as the release {RELEASE_TAG}")
            return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"sandbox-ai packages: {printable(exc, 1000)}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
