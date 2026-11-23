"""sandbox-ai mcp add NAME --build: a catalog server that has no signed image yet, built here from its reviewed source.

A catalog entry without a digest ("pending": reviewed, its image not published and signed yet) is refused by a plain
`mcp add`. With --build it is built on this host instead, exactly as the catalog's CI builds it, and added UNSIGNED:

  1. the source: the catalog's own repository at an exact commit, never a moving branch - for a catalog on GitHub
     (https://raw.githubusercontent.com/OWNER/REPO/REF/catalog.json) REF resolved to its commit once, and the catalog
     read again at that commit (its signature too: the entry built is the entry of that commit); for a catalog file
     in a git checkout its HEAD, unchanged (catalog.json and servers/NAME as committed). The source archive (GitHub's
     archive of the commit, or `git archive` of it) is hashed, and the hash is shown;
  2. the human confirms what is built (the entry, the repository, the commit, the archive's SHA-256, unsigned) -
     on a terminal, or with --yes after the question was asked elsewhere (the app, the web interface: they pass
     --commit and --source-sha256 of what they showed, and nothing else is built);
  3. servers/NAME is taken out of the archive (regular files and folders only, nothing outside it), its Dockerfile
     checked (every base image pinned by digest), and built with this host's Podman (linux/amd64, as CI);
  4. the image is started with sandbox-ai's own run line (mcp.image_command) and its smoke spec run against it
     (servers/NAME/smoke.json, tools.json: initialize, tools/list exactly the recorded tools, the real and refused
     calls; a call that needs a secret not given here is left out and said) - as CI's "Smoke with sandbox-ai under
     Podman" does;
  5. it is added as `mcp add --unsigned` adds a server: "signed_by": null, and "built" records the commit, the
     repository and the archive's hash. Everywhere it is shown it says "built here, not signed"; nothing ever
     presents it as signed. A build or smoke run that fails is shown with its log (and the log's file), the image
     is removed again, and nothing is added.

Nothing here weakens the signature check of a signed entry: an entry that has a digest is refused (`mcp add NAME`
pulls its signed image), a catalog whose signature does not verify is refused as `mcp add` refuses it, and one that
is not signed needs --unsigned here too.
"""
import base64
import binascii
import contextlib
import datetime
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import tarfile
import tempfile
import time

HERE = Path(__file__).resolve().parent

GITHUB_CATALOG = re.compile(r"https://raw\.githubusercontent\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})/"
                            r"([A-Za-z0-9_./-]{1,200})/catalog\.json")
COMMIT = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
IMAGE_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
IMAGE_PREFIX = "localhost/sandbox-ai-mcp-"
PLATFORM = "linux/amd64"  # what CI builds (and what the Dockerfiles name: java-21-openjdk-amd64 ...)
ARCHIVE_LIMIT = 128 << 20  # the repository's archive (sandbox-ai-mcp's: a few MiB)
TAKEN_LIMIT = 64 << 20  # what is taken out of it: one server's folder and the fixtures
TAKEN_FILES = 4000
FIXTURES = "tests/fixtures/analysis-files.json"
FIXTURE_LIMIT = 4 << 20
FIXTURE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
LOG_LINES = 40  # of a failed build, said with the failure (the whole log is in its file)
VARIABLE = re.compile(r"\$\{([A-Z0-9_]+)\}")
PROTOCOL_VERSION = "2025-06-18"

m = None  # mcp.py's names (bind)


def bind(names):
    """Use mcp.py's NAMES (its globals(), read as they are now)."""
    global m

    class Names:
        def __getattr__(self, name):
            try:
                return names[name]
            except KeyError:
                raise AttributeError(name) from None
    m = Names()


class BuildFailed(ValueError):
    """A build or smoke run that failed: its words (SUMMARY, one line), the log's file and its last lines."""

    def __init__(self, summary, log=None):
        self.summary, self.log = summary, log
        self.tail = log_tail(log) if log else ""
        super().__init__(summary + (f" (the whole log: {log})" if log else "")
                         + (f"\nthe log's end:\n{self.tail}" if self.tail else ""))


# --- where the source is -----------------------------------------------------------------------------------------

def http_get(url, limit, accept=None, fetch=None):
    """The bytes of URL (at most LIMIT + 1); FETCH(url, headers) for the tests."""
    headers = {"User-Agent": "sandbox-ai", **({"Accept": accept} if accept else {})}
    if fetch is not None:
        return fetch(url, headers)
    import urllib.request
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read(limit + 1)


# Git in a local catalog's checkout runs on the host, and such a checkout may lie in a workspace, the agent's to write:
# nothing its configuration names may run - no hooks, fsmonitor, pager, transport, replacement objects - and no command
# that hashes the work tree or writes files through the attributes' filters (git status runs a clean filter, git archive
# a smudge filter): plumbing only (git_tree, git_blobs); the comparison (worktree_changes) and the archive
# (git_archive) are made here.
GIT_SAFE = ("--no-pager", "--no-replace-objects", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
            "-c", "protocol.allow=never", "-c", "core.sshCommand=false", "-c", "core.alternateRefsCommand=")


def git(directory, *args, run=subprocess.run, binary=False, data=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C", GIT_NO_LAZY_FETCH="1",
               GIT_ALLOW_PROTOCOL="sandbox-ai-none")
    done = run(["git", *GIT_SAFE, "-C", str(directory), *args], capture_output=True, timeout=120, check=False,
               env=env, **({"input": data} if data is not None else {"stdin": subprocess.DEVNULL}),
               **({} if binary else {"text": True}))
    if done.returncode != 0:
        said = done.stderr if isinstance(done.stderr, str) else (done.stderr or b"").decode("utf-8", "replace")
        raise ValueError(f"git {' '.join(args[:2])} in {directory}: {said.strip()[-300:] or 'failed'}")
    return done.stdout


def git_tree(directory, commit, paths, run=subprocess.run):
    """{path: (mode, object id)} of the files below PATHS in COMMIT (ls-tree: nothing read from the work tree)."""
    found = {}
    for record in git(directory, "ls-tree", "-r", "-z", "--full-tree", commit, "--", *paths, run=run,
                      binary=True).split(b"\0"):
        if record:
            meta, _, path = record.partition(b"\t")
            mode, kind, oid = meta.decode("ascii").split()
            found[path.decode("utf-8", "surrogateescape")] = (mode, oid)
    return found


def git_blobs(directory, oids, run=subprocess.run):
    """{object id: its bytes} of the blobs OIDS, as stored (cat-file --batch: no filter, no conversion)."""
    oids = list(dict.fromkeys(oids))
    if not oids:
        return {}
    raw = git(directory, "cat-file", "--batch", run=run, binary=True, data="".join(f"{oid}\n" for oid in oids).encode())
    found, position = {}, 0
    while position < len(raw):
        end = raw.index(b"\n", position)
        oid, kind, size = raw[position:end].decode("ascii").split()
        found[oid] = raw[end + 1:end + 1 + int(size)]
        position = end + 1 + int(size) + 1
    return found


def worktree_changes(directory, commit, paths, run=subprocess.run):
    """[path] below PATHS whose file in the work tree is not as COMMIT has it, or that COMMIT does not have (and Git
    does not ignore): read and hashed here as the bytes are (git status would run the repository's filters)."""
    directory = Path(directory)
    tree = git_tree(directory, commit, paths, run)
    hasher = hashlib.sha256 if git(directory, "rev-parse", "--show-object-format", run=run).strip() == "sha256" \
        else hashlib.sha1
    changed = [path for path in git(directory, "ls-files", "-z", "-o", "--exclude-standard", "--", *paths, run=run,
                                    binary=True).decode("utf-8", "surrogateescape").split("\0") if path]
    for path, (mode, oid) in tree.items():
        if mode == "160000":
            continue  # a submodule: no file of this checkout
        file = directory / path
        try:
            data = os.readlink(file).encode("utf-8", "surrogateescape") if mode == "120000" else \
                (file.read_bytes() if not file.is_symlink() else None)
        except OSError:
            data = None
        if data is None or hasher(b"blob %d\0" % len(data) + data).hexdigest() != oid or \
                (mode == "120000") != file.is_symlink():
            changed.append(path)
    return sorted(set(changed))


def git_archive(directory, commit, prefix, paths, run=subprocess.run):
    """A tar archive of the files below PATHS in COMMIT under PREFIX (as git archive --format=tar makes one, but from
    the blobs as stored: no smudge filter of the repository runs). The same commit gives the same bytes."""
    tree = git_tree(directory, commit, paths, run)
    blobs = git_blobs(directory, [oid for mode, oid in tree.values() if mode != "160000"], run)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(tree):
            mode, oid = tree[path]
            if mode == "160000":
                continue
            info = tarfile.TarInfo(prefix + path)
            info.mtime, info.uname, info.gname = 0, "root", "root"
            if mode == "120000":
                info.type, info.linkname, info.mode = tarfile.SYMTYPE, blobs[oid].decode("utf-8", "surrogateescape"), \
                    0o777
                archive.addfile(info)
                continue
            info.size, info.mode = len(blobs[oid]), 0o755 if mode == "100755" else 0o644
            archive.addfile(info, io.BytesIO(blobs[oid]))
    return buffer.getvalue()


def github_commit(owner, repo, ref, fetch=None):
    """The commit REF (a branch, a tag or a commit) of github.com/OWNER/REPO is now (GitHub's API)."""
    if COMMIT.fullmatch(ref):
        return ref
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}"
    try:
        found = http_get(url, 200, accept="application/vnd.github.sha", fetch=fetch)
    except Exception as exc:  # noqa: BLE001 - said: nothing is built without the commit
        reason = m.os_reason(exc) if isinstance(exc, OSError) else exc
        raise ValueError(f"the commit of {owner}/{repo} {ref} could not be read ({url}: {reason}): nothing is built "
                         "without it") from None
    commit = found.decode("ascii", "replace").strip() if isinstance(found, bytes) else str(found).strip()
    if not COMMIT.fullmatch(commit):
        raise ValueError(f"{url}: not a commit ({m.launcher().printable(commit[:80])})")
    return commit


def source_of(catalog, name, run=subprocess.run, fetch=None):
    """Where the source of the entry NAME of CATALOG (its URL or file) is, at an exact commit: {"kind": "github" |
    "git", "repository" (for people and the image's label), "commit", "catalog" (the catalog at that commit: what the
    entry is read from), "path": "servers/NAME", and "owner", "repo" or "directory"}. ValueError for a catalog that
    names no such source."""
    github = GITHUB_CATALOG.fullmatch(catalog)
    if github:
        owner, repo, ref = github.groups()
        commit = github_commit(owner, repo, ref, fetch)
        return {"kind": "github", "owner": owner, "repo": repo, "repository": f"https://github.com/{owner}/{repo}",
                "commit": commit, "path": f"servers/{name}",
                "catalog": f"https://raw.githubusercontent.com/{owner}/{repo}/{commit}/catalog.json"}
    if catalog.startswith("https://"):
        raise ValueError(f"the catalog {catalog} is not one of a GitHub repository (raw.githubusercontent.com/OWNER/"
                         "REPO/REF/catalog.json): there is no source at an exact commit to build from")
    path = Path(os.path.expanduser(catalog)).resolve()
    directory = path.parent
    try:
        top = Path(git(directory, "rev-parse", "--show-toplevel", run=run).strip()).resolve()
    except (OSError, subprocess.SubprocessError, ValueError):
        top = None
    if top != directory or path.name != "catalog.json":
        raise ValueError(f"the catalog {catalog} is not the catalog.json of a git checkout (its top folder): there is "
                         "no source at an exact commit to build from")
    commit = git(directory, "rev-parse", "HEAD", run=run).strip()
    if not COMMIT.fullmatch(commit):
        raise ValueError(f"{directory}: HEAD is not a commit")
    changed = worktree_changes(directory, commit, ["catalog.json", f"servers/{name}", FIXTURES], run)
    if changed:
        raise ValueError(f"{directory}: catalog.json or servers/{name} differ from the commit {commit[:12]} "
                         f"({m.launcher().printable(changed[0])[:120]}): only what is committed is built "
                         "(commit it, or check it out clean)")
    return {"kind": "git", "directory": str(directory), "repository": str(directory), "commit": commit,
            "path": f"servers/{name}", "catalog": str(path)}


def plan(name, catalogs=None, fetch=None, unsigned=False, commit=None, run=subprocess.run, http=None):
    """What `mcp add NAME --build` builds, checked before anything is downloaded or built: {"entry": PREFIX:NAME,
    "name", "prefix", "catalog" (at the commit), "catalog_label", "repository", "commit", "path", "signature" (good:
    the catalog that lists it for review is signed - the image built here is not; unsigned: taken with --unsigned),
    "signer", "network", "image" (the local name it gets), "kind", "owner"/"repo"/"directory"} and the entry itself
    (under "_entry"). COMMIT: only that commit (what was asked: the catalog moved on since, refused)."""
    entries, problems = m.catalog_entries(catalogs, fetch)
    for problem in problems:
        m.LOG.warning("mcp", f"catalog not read: {problem}")
    entry = m.find_entry(entries, name)
    prefix = next(p for p, e in entries if e is entry)
    bare = entry.get("name")
    published(entry)
    catalog = entry.get("_catalog")
    if not isinstance(catalog, str):
        raise ValueError(f"{bare}: its catalog is not known, so there is no source to build it from")
    if not IMAGE_NAME.fullmatch(str(bare)) or len(bare) > 32:
        raise ValueError(f"{bare}: not a name an image of it can have (lower-case letters, digits and -)")
    source = source_of(catalog, bare, run=run, fetch=http)
    if commit is not None and commit != source["commit"]:
        raise ValueError(f"{bare}: the catalog is at the commit {source['commit'][:12]} now, not at {commit[:12]} "
                         "that was asked about: nothing is built (ask again)")
    if source["catalog"] != catalog:
        # The entry of that very commit, signature and all: what is built is what that commit's catalog says.
        entries, problems = m.catalog_entries([(prefix, source["catalog"])], fetch)
        for problem in problems:
            m.LOG.warning("mcp", f"catalog not read: {problem}")
        if problems and not entries:
            raise ValueError(f"{bare}: the catalog at the commit {source['commit'][:12]} could not be read: "
                             + "; ".join(problems))
        entry = m.find_entry(entries, f"{prefix}:{bare}")
        published(entry)
    signed = m.check_signature(entry, unsigned, out=lambda line: None)
    return {"entry": f"{prefix}:{bare}", "name": bare, "prefix": prefix, "catalog": source["catalog"],
            "catalog_label": m.catalog_label(source["catalog"]), "title": str(entry.get("title") or bare),
            "signature": "good" if signed.get("signed_by") else "unsigned", "signer": signed.get("signed_by"),
            "network": str(entry.get("network") or "none"), "image": IMAGE_PREFIX + bare,
            **{key: value for key, value in source.items() if key != "catalog"}, "_entry": entry}


def published(entry):
    """Refused: an entry that has an image (its digest) - `mcp add` pulls that one, checked against its signature."""
    if isinstance(entry.get("digest"), str) and entry["digest"]:
        raise ValueError(f"{entry.get('name')} has a published image in its catalog: sandbox-ai mcp add "
                         f"{entry.get('name')} adds it (pulled by its digest, its catalog's signature checked) - "
                         "nothing to build")
    if entry.get("kind") not in (None, "oci"):
        raise ValueError(f"{entry.get('name')}: a catalog entry of kind {entry.get('kind')}, not a container image: "
                         "nothing to build")


def plan_json(found):
    """The plan as `mcp add --build-plan` prints it (the entry itself left out)."""
    return {key: value for key, value in found.items() if not key.startswith("_")}


# --- the source archive --------------------------------------------------------------------------------------------

def cache_dir():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "sandbox-ai" / "mcp-build"


def log_dir():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "sandbox-ai" / "mcp-build"


def archive_of(found, run=subprocess.run, http=None, out=print):
    """The source archive of FOUND (plan) as bytes, and found["archive"], found["archive_sha256"] set: GitHub's archive
    of the commit (kept in cache_dir() by its commit, so the build after the question takes the bytes the question
    named), or `git archive` of the checkout's commit."""
    if found["kind"] == "git":
        data = git_archive(found["directory"], found["commit"], "source/", [found["path"], FIXTURES], run)
        found["archive"] = f"git archive {found['commit']} ({found['directory']})"
    else:
        url = f"https://codeload.github.com/{found['owner']}/{found['repo']}/tar.gz/{found['commit']}"
        found["archive"] = url
        cached = cache_dir() / f"{found['owner']}-{found['repo']}-{found['commit']}.tar.gz"
        data = None
        with contextlib.suppress(OSError):
            data = cached.read_bytes()
        if not data:
            out(f"sandbox-ai mcp: downloading the source of {found['name']}: {url}")
            try:
                data = http_get(url, ARCHIVE_LIMIT, fetch=http)
            except Exception as exc:  # noqa: BLE001 - said; nothing is built
                raise ValueError(f"{found['name']}: its source could not be downloaded ({url}: {exc})") from None
            if len(data) > ARCHIVE_LIMIT:
                raise ValueError(f"{url}: larger than {ARCHIVE_LIMIT >> 20} MiB")
            with contextlib.suppress(OSError):
                cached.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                partial = cached.with_name(cached.name + f".{os.getpid()}")
                partial.write_bytes(data)
                os.chmod(partial, 0o600)
                os.replace(partial, cached)
    found["archive_sha256"] = hashlib.sha256(data).hexdigest()
    return data


def extract(data, found, destination):
    """servers/NAME (and the smoke fixtures) of the archive DATA, written below DESTINATION: {"server": its folder,
    "fixtures": the fixtures' file or None}. Only regular files and folders inside them; a link, a device, an
    absolute path or one with .. anywhere among them, or more than TAKEN_LIMIT bytes, is refused (nothing of the
    archive runs here: Podman builds the folder)."""
    wanted = found["path"].split("/")
    fixtures = FIXTURES.split("/")
    taken = size = 0
    destination = Path(destination)
    server = destination / "server"
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError(f"{found['name']}: the source archive cannot be read ({exc})") from None
    fixture_file = None
    with archive:
        top = None
        for member in archive:
            parts = member.name.split("/")
            while parts and parts[-1] == "":
                parts.pop()
            if not parts or member.name.startswith("/") or any(p in ("", ".", "..") for p in parts):
                raise ValueError(f"{found['name']}: the source archive holds a path outside it "
                                 f"({m.launcher().printable(member.name)[:200]})")
            top = parts[0] if top is None else top
            if parts[0] != top:
                raise ValueError(f"{found['name']}: the source archive has more than one top folder")
            rest = parts[1:]
            if rest[:len(wanted)] == wanted:
                target = server.joinpath(*rest[len(wanted):]) if len(rest) > len(wanted) else server
            elif rest == fixtures:
                target = destination / "fixtures.json"
            else:
                continue
            if not (member.isreg() or member.isdir()):
                raise ValueError(f"{found['name']}: the source archive holds {'/'.join(rest)}, which is no regular "
                                 "file or folder (a link or a device): nothing is built")
            taken += 1
            size += member.size if member.isreg() else 0
            if taken > TAKEN_FILES or size > TAKEN_LIMIT:
                raise ValueError(f"{found['name']}: {found['path']} is larger than {TAKEN_LIMIT >> 20} MiB or "
                                 f"{TAKEN_FILES} files")
            if member.isdir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            with open(target, "xb") as handle:
                shutil.copyfileobj(stream, handle)
            os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
            if target == destination / "fixtures.json":
                fixture_file = target
    if not server.is_dir():
        raise ValueError(f"{found['name']}: the source at {found['commit'][:12]} has no {found['path']}: there is "
                         "nothing to build")
    return {"server": server, "fixtures": fixture_file}


# --- the Dockerfile ------------------------------------------------------------------------------------------------

def containerfile(server):
    for name in ("Containerfile", "Dockerfile"):
        if (Path(server) / name).is_file():
            return Path(server) / name
    raise ValueError(f"{Path(server).name}: no Containerfile or Dockerfile in its folder: nothing to build")


def instructions(text):
    """(INSTRUCTION, ARGUMENTS) of a Dockerfile's TEXT: comments left out, continued lines joined."""
    found, current = [], ""
    for line in text.splitlines():
        stripped = line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if current and stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            current += stripped[:-1] + " "
            continue
        current += stripped
        word, _, rest = current.partition(" ")
        found.append((word.upper(), rest.strip()))
        current = ""
    if current.strip():
        word, _, rest = current.strip().partition(" ")
        found.append((word.upper(), rest.strip()))
    return found


def base_images(text):
    """[image] every FROM (and COPY --from, RUN --mount=...,from=) of the Dockerfile TEXT pulls, with its ARG
    defaults put in (no --build-arg is given). ValueError for one that is not pinned by digest (NAME@sha256:...),
    one whose ARG has no default, and a build stage named before it is made."""
    defaults, stages, images = {}, set(), []

    def resolved(value):
        def one(match):
            key = match.group(1) or match.group(2)
            if key not in defaults:
                raise ValueError(f"the base image {value} names ${key}, which has no default (ARG {key}=...)")
            return defaults[key]
        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", one, value)

    def pinned(value, where):
        image = resolved(value)
        if image in stages or image == "scratch" or re.fullmatch(r"[0-9]+", image):
            return
        if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
            raise ValueError(f"{where} {image}: not pinned by digest (NAME@sha256:...): nothing is built from a base "
                             "image that can change")
        images.append(image)
    for word, rest in instructions(text):
        if word == "ARG":
            for item in rest.split():
                key, equals, value = item.partition("=")
                if equals and key not in defaults:
                    defaults[key] = value.strip('"\'')
        elif word == "FROM":
            words = [w for w in rest.split() if not w.startswith("--")]
            if not words:
                raise ValueError("a FROM without an image")
            pinned(words[0], "FROM")
            if len(words) >= 3 and words[1].lower() == "as":
                stages.add(words[2])
        elif word in ("COPY", "ADD"):
            for flag in rest.split():
                if flag.startswith("--from="):
                    pinned(flag[len("--from="):], "COPY --from")
        elif word == "RUN":
            for flag in rest.split():
                if flag.startswith("--mount="):
                    for part in flag[len("--mount="):].split(","):
                        if part.startswith("from="):
                            pinned(part[len("from="):], "RUN --mount from")
    if not images and not stages:
        raise ValueError("the Dockerfile has no FROM")
    return list(dict.fromkeys(images))


# --- the build -----------------------------------------------------------------------------------------------------

def streamed(argv, log, out, popen=subprocess.Popen, env=None):
    """ARGV run, each line of its output written to LOG (a file) and said (OUT, indented); its exit status."""
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(argv) + "\n")
        handle.flush()
        process = popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        for raw in iter(process.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\n")
            handle.write(line + "\n")
            out("  | " + m.launcher().printable(line)[:400])
        process.stdout.close()
        return process.wait()


def log_tail(log, lines=LOG_LINES):
    try:
        text = Path(log).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def build_image(found, server, log, out=print, popen=subprocess.Popen, run=subprocess.run):
    """FOUND's server folder SERVER built with Podman as CI builds it (its context, linux/amd64, the source label);
    the image's reference by digest (localhost/sandbox-ai-mcp-NAME@sha256:...). BuildFailed with the log's end."""
    file = containerfile(server)
    images = base_images(file.read_text(encoding="utf-8", errors="replace"))
    tag = f"{found['image']}:build-{found['commit'][:12]}"
    out(f"sandbox-ai mcp: building {found['name']} with Podman ({PLATFORM}; base images pinned: "
        f"{', '.join(images) or 'none'}); the whole output goes to {log}")
    argv = ["podman", "build", f"--platform={PLATFORM}", "--pull=missing",
            "--label", f"org.opencontainers.image.source={found['repository']}" if found["kind"] == "github"
            else "org.opencontainers.image.source=local",
            "--label", f"sandbox-ai.mcp.commit={found['commit']}", "--label", "sandbox-ai.mcp.built=here",
            "--label", "sandbox-ai.mcp.signed=no",
            "--tag", tag, "--file", str(file), str(server)]
    begun = time.monotonic()
    status = streamed(argv, log, out, popen)
    m.LOG.ran("mcp", f"build the image of {found['name']} at {found['commit'][:12]}", argv, begun, returncode=status)
    if status != 0:
        raise BuildFailed(f"{found['name']} is not added: its build failed (exit status {status})", log)
    done = run(["podman", "image", "inspect", "--format", "{{json .RepoDigests}} {{.Digest}}", tag],
               capture_output=True, text=True, timeout=60, check=False, stdin=subprocess.DEVNULL)
    digest = None
    if done.returncode == 0:
        listed, _, own = done.stdout.strip().rpartition(" ")
        with contextlib.suppress(ValueError, RecursionError):
            for item in json.loads(listed or "[]") or []:
                if isinstance(item, str) and item.startswith(found["image"] + "@sha256:"):
                    digest = item.rpartition("@")[2]
                    break
        if digest is None and re.fullmatch(r"sha256:[0-9a-f]{64}", own):
            digest = own
    if digest is None or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise BuildFailed(f"{found['name']} is not added: Podman gave its built image no digest "
                          f"({(done.stderr or done.stdout or '').strip()[-200:]})", log)
    ref = f"{found['image']}@{digest}"
    exists = run(["podman", "image", "exists", ref], capture_output=True, timeout=60, check=False,
                 stdin=subprocess.DEVNULL)
    if exists.returncode != 0:
        raise BuildFailed(f"{found['name']} is not added: Podman does not find the built image by its digest ({ref})",
                          log)
    return ref, tag


def remove_image(tag, run=subprocess.run):
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        run(["podman", "image", "rm", "--force", tag], capture_output=True, timeout=120, check=False,
            stdin=subprocess.DEVNULL)


# --- the smoke run (CI's "Smoke with sandbox-ai under Podman", with sandbox-ai's own run line) -----------------------

def substitute(value, variables, missing=None):
    if isinstance(value, str):
        def one(match):
            key = match.group(1)
            if key not in variables:
                if missing is not None:
                    return missing
                raise KeyError(key)
            return variables[key]
        return VARIABLE.sub(one, value)
    if isinstance(value, list):
        return [substitute(v, variables, missing) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, variables, missing) for k, v in value.items()}
    return value


def names_in(value):
    return set(VARIABLE.findall(json.dumps(value)))


def write_fixtures(path, folder):
    """The binary smoke fixtures of FIXTURES' file PATH written into FOLDER, each checked against its SHA-256 (as the
    catalog's scripts/fixtures.py does; nothing ever runs them: the servers analyse them in their containers)."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError) as exc:
        raise ValueError(f"{FIXTURES}: not readable ({exc})") from None
    if not isinstance(document, dict) or not isinstance(document.get("files"), list):
        raise ValueError(f"{FIXTURES}: expected {{\"files\": [...]}}")
    for entry in document["files"]:
        name = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not FIXTURE_NAME.fullmatch(name):
            raise ValueError(f"{FIXTURES}: {name!r} is not a plain file name")
        try:
            data = gzip.decompress(base64.b64decode(entry.get("gzip_base64") or "", validate=True))
        except (binascii.Error, OSError, ValueError, TypeError, EOFError) as exc:
            raise ValueError(f"{FIXTURES}: {name}: not base64 of a gzip ({exc})") from None
        if len(data) > FIXTURE_LIMIT or hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise ValueError(f"{FIXTURES}: {name}: its bytes do not hash to the SHA-256 it pins (or it is too large)")
        target = Path(folder) / name
        target.write_bytes(data)
        os.chmod(target, 0o644)


def seed(host, items, target):
    """A fresh mount's "seed" (smoke_test.py's): files and links put into HOST."""
    if not isinstance(items, list):
        raise ValueError(f"mount {target}: seed is a list")
    for item in items:
        path = item.get("path") if isinstance(item, dict) else None
        parts = path.split("/") if isinstance(path, str) else []
        if not parts or path.startswith("/") or any(p in ("", ".", "..") for p in parts) or \
                len(set(item) & {"text", "symlink"}) != 1 or not isinstance(item.get("text", item.get("symlink")), str):
            raise ValueError(f"mount {target}: a seed item is {{\"path\": RELATIVE, \"text\" or \"symlink\": TEXT}}")
        where = os.path.join(host, *parts)
        for depth in range(1, len(parts)):
            folder = os.path.join(host, *parts[:depth])
            os.makedirs(folder, exist_ok=True)
            os.chmod(folder, 0o777)
        if "symlink" in item:
            os.symlink(item["symlink"], where)
        else:
            with open(where, "x", encoding="utf-8") as handle:
                handle.write(item["text"])
            os.chmod(where, 0o666)


HOST_PREFIXES = ("CONTAINER", "PODMAN", "BUILDAH", "STORAGE", "REGISTRIES", "XDG_", "DBUS_", "SSH_", "SSL_", "GIT_",
                 "TMP", "LC_", "LANG")


def host_variable(name):
    """Why the smoke spec's variable NAME may not be set for the podman that starts the server (its value reaches the
    container through Podman's own environment, as sandbox-ai's run line passes variables), else None: one this
    environment has already, one Podman, the dynamic loader or an interpreter on this host reads (LD_PRELOAD,
    CONTAINERS_CONF, HTTPS_PROXY, ...)."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
        raise ValueError(f"{name!r} is not a variable's name")
    upper = name.upper()
    if name in os.environ or m.code_variable(name) or upper.startswith(HOST_PREFIXES) or upper.endswith("_PROXY") \
            or upper in ("PATH", "HOME", "SHELL", "USER", "LOGNAME", "TERM", "TZ", "IFS", "ENV", "BASH_ENV"):
        return "a variable this host's own programs read, never set from a catalog's smoke spec"
    return None


class Session:
    """JSON-RPC over the server's standard input and output (one line each)."""

    def __init__(self, argv, env, stderr):
        # (its standard error straight into the log: a pipe nobody reads fills, and the server stops)
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, env=env)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.next_id = 1

    def send(self, message):
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()

    def request(self, method, params, timeout):
        ident = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except (ValueError, RecursionError):
                    raise RuntimeError(f"not JSON-RPC on its standard output: {line[:200]!r}") from None
                if isinstance(message, dict) and message.get("id") == ident and \
                        ("result" in message or "error" in message):
                    return message
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{method}: no answer within {timeout:g} s")
            if not self.selector.select(left):
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError(f"{method}: the server closed its output (exit status {self.proc.poll()})")
            self.buffer += chunk

    def close(self):
        with contextlib.suppress(OSError):
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        with contextlib.suppress(OSError):
            self.proc.stdout.close()


def result_text(message):
    if "error" in message:
        return "JSON-RPC error: " + json.dumps(message["error"])
    result = message.get("result") if isinstance(message.get("result"), dict) else {}
    content = result.get("content") or []
    return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))


def smoke(found, server, fixtures, image, log, out=print):
    """IMAGE started with sandbox-ai's own run line (mcp.image_command of its network plan) and checked with the
    server's smoke spec (servers/NAME/smoke.json, tools.json); BuildFailed when a check fails. A call that needs a
    variable this host does not give (a secret of CI's) is left out, and said."""
    spec_file, tools_file = Path(server) / "smoke.json", Path(server) / "tools.json"
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        recorded = json.loads(tools_file.read_text(encoding="utf-8"))["tools"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise BuildFailed(f"{found['name']} is not added: its smoke spec cannot be read (servers/{found['name']}/"
                          f"smoke.json and tools.json: {exc}), so the image built cannot be checked", log) from None
    if not isinstance(spec, dict) or not isinstance(recorded, list):
        raise BuildFailed(f"{found['name']} is not added: its smoke spec is not one", log)
    own = {}
    with contextlib.suppress(OSError, ValueError, RecursionError):
        entry = json.loads((Path(server) / "server.json").read_text(encoding="utf-8"))
        if isinstance(entry, dict) and entry.get("pid1") == "server":
            own["pid1"] = "server"
    limits = {key: spec[key] for key in ("memory", "cpus", "pids", "tmp") if key in spec}
    scratch = Path(tempfile.mkdtemp(prefix=f"sandbox-ai-mcp-smoke-{found['name']}-"))
    variables = {}
    try:
        if "FIXTURES" in names_in(spec) and fixtures is not None:
            folder = scratch / "fixtures"
            folder.mkdir()
            write_fixtures(fixtures, folder)
            os.chmod(folder, 0o755)
            variables["FIXTURES"] = str(folder)
        needed = names_in({key: spec.get(key) for key in ("mounts", "calls")}) - set(variables)
        list_only = bool(needed)
        if list_only:
            out(f"sandbox-ai mcp: smoke run of {found['name']}: its calls need {', '.join(sorted(needed))} (CI's), "
                "which this host does not give the check: only its start and its tools are checked")
        network = spec.get("network", "none")
        network = "none" if list_only and network == "allowlist" else \
            "full" if network in ("full", "allowlist") else "none"
        mounts = []
        for index, mount in enumerate([] if list_only else spec.get("mounts", [])):
            if not isinstance(mount, dict) or not isinstance(mount.get("target"), str):
                raise ValueError("a mount of the smoke spec is {target, host or fresh}")
            if mount.get("fresh"):
                host = scratch / f"fresh-{index}"
                host.mkdir()
                os.chmod(host, 0o777)
                seed(str(host), mount.get("seed", []), mount["target"])
            else:
                host = substitute(mount.get("host"), variables)
            mounts.append({"target": mount["target"], "host": str(host),
                           "mode": "rw" if mount.get("writable") else "ro"})
        env = {str(k): str(v) for k, v in substitute(spec.get("env") or {}, variables, "").items()}
        for name in env:
            problem = host_variable(name)
            if problem:
                raise ValueError(f"its env {name}: {problem}")
        definition = m.validated_image({"image": image, "network": network, "env": sorted(env),
                                        "mounts": mounts, **limits, **own}, f"smoke run of {found['name']}")
        argv = m.image_command(m.server_network(f"build-{found['name']}", definition))
        with open(log, "a", encoding="utf-8") as handle:
            handle.write("$ " + " ".join(argv) + "\n")
        out(f"sandbox-ai mcp: smoke run of {found['name']} with sandbox-ai's own run line (network {network})")
        failures = run_checks(spec, recorded, argv, {**os.environ, **env}, variables, list_only, log, out)
    except (KeyError, ValueError) as exc:
        failures = [f"the smoke spec: {exc}"]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if failures:
        raise BuildFailed(f"{found['name']} is not added: the image built does not pass its smoke test: "
                          + "; ".join(failures), log)


def run_checks(spec, recorded, argv, environment, variables, list_only, log, out):
    """[failure] of the smoke checks of SPEC against the server ARGV starts (initialize, tools/list against RECORDED and
    spec's expect_tools, then its calls unless LIST_ONLY)."""
    failures = []
    with open(log, "a", encoding="utf-8") as handle:
        handle.write("--- the server's standard error follows the calls ---\n")
    stderr = open(log, "ab")  # noqa: SIM115 - closed below, after the server ended
    try:
        session = Session(argv, environment, stderr)
    except OSError as exc:
        stderr.close()
        return [f"the server could not be started: {exc}"]
    try:
        init = session.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                              "clientInfo": {"name": "sandbox-ai-mcp-build", "version": "1"}},
                               float(spec.get("start_timeout", 60)))
        if "result" not in init:
            failures.append(f"initialize: {result_text(init)[:300]}")
            return failures
        session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = session.request("tools/list", {}, 60)
        tools = (listed.get("result") or {}).get("tools") or [] if isinstance(listed.get("result"), dict) else []
        names = [t.get("name") for t in tools if isinstance(t, dict)]
        missing = [t for t in spec.get("expect_tools", []) if t not in names]
        added = sorted(set(map(str, names)) - set(map(str, recorded)))
        gone = sorted(set(map(str, recorded)) - set(map(str, names)))
        if missing or added or gone:
            failures.append("tools/list does not offer what tools.json records"
                            + (f" (not recorded: {', '.join(added)})" if added else "")
                            + (f" (missing: {', '.join(gone or missing)})" if gone or missing else ""))
        else:
            out(f"sandbox-ai mcp: smoke: tools/list offers its {len(names)} recorded tools")
        for call in [] if list_only else spec.get("calls", []):
            arguments = substitute(call.get("arguments", {}), variables)
            expect = substitute(call.get("expect", ""), variables)
            began = time.monotonic()
            answer = session.request("tools/call", {"name": call["tool"], "arguments": arguments},
                                     float(call.get("timeout", 120)))
            text = result_text(answer)
            passed = expect in text and "error" not in answer
            if call.get("expect_error"):
                passed = expect in text
            limited = None if passed or "error" in answer else \
                next((marker for marker in call.get("upstream_limited") or [] if marker in text), None)
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(f"tools/call {call['tool']} {json.dumps(arguments)[:300]}: "
                             f"{'ok' if passed else 'skipped' if limited else 'FAILED'}\n{text[:1500]}\n")
            took = time.monotonic() - began
            if limited:
                out(f"sandbox-ai mcp: smoke: {call['tool']}: its service refused for its own reasons ({limited}); "
                    "not counted against it")
            elif passed:
                out(f"sandbox-ai mcp: smoke: {call['tool']}: ok ({took:.1f} s)")
            else:
                failures.append(f"tools/call {call['tool']}: expected {expect!r}, got "
                                f"{m.launcher().printable(' '.join(text.split()))[:300]!r}")
    except (OSError, RuntimeError, TimeoutError, KeyError, TypeError, ValueError) as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        session.close()
        stderr.close()
    return failures


# --- all of it -----------------------------------------------------------------------------------------------------

def question(found):
    """What the human is asked before anything is built."""
    reviewed = (f"its catalog ({found['catalog_label']}) is signed by {found['signer']}: the entry was reviewed, but "
                "no signed image of it is published yet" if found["signature"] == "good" else
                f"its catalog ({found['catalog_label']}) is NOT signed (--unsigned): nobody vouched for the entry")
    return (f"{found['entry']} ({found['title']}) has no signed image yet; {reviewed}. Build it here with Podman from "
            f"its source - {found['repository']} at the commit {found['commit']}, {found['path']} (the source "
            f"archive's SHA-256: {found['archive_sha256']}) - check it with its smoke test, and add it UNSIGNED "
            "(built here, not signed: nobody signed this image)?")


def build_and_add(name, add_options, catalogs=None, fetch=None, unsigned=False, commit=None, source_sha256=None,
                  confirm=None, out=print, run=subprocess.run, popen=subprocess.Popen, http=None, plan_only=False):
    """`mcp add NAME --build` (module docstring): the definition added, or (PLAN_ONLY, --build-plan) the plan with
    the archive's hash, nothing built. ADD_OPTIONS: mcp.add's keyword arguments (mounts, env, network, limits, vpn,
    local, ...). CONFIRM(question) -> bool: the human's answer (False, or None without one: nothing is built)."""
    if add_options.get("backend") not in (None, "podman"):
        raise ValueError("--build builds an image for Podman: --backend bwrap needs the published rootfs of a signed "
                         "entry")
    found = plan(name, catalogs, fetch, unsigned, commit, run=run, http=http)
    entry = found.pop("_entry")
    m.LOG.info("mcp", f"build asked: {found['entry']} at {found['commit']} ({found['repository']})")
    # What add would refuse (a folder it needs, a variable, the name) is refused before anything is downloaded.
    local = add_options.get("local") or found["name"]
    if not re.fullmatch(m.MCP_NAME_PATTERN, local) or local in m.BUILTIN:
        raise ValueError(f"invalid or reserved server name: {local}")
    m.definition_from_entry({**entry, "kind": "oci", "image": found["image"], "digest": "sha256:" + "0" * 64},
                            add_options.get("mounts", ()), add_options.get("env", ()), add_options.get("network"),
                            add_options.get("memory"), add_options.get("pids"), add_options.get("vpn"),
                            cpus=add_options.get("cpus"), tmp=add_options.get("tmp"),
                            allow_private=add_options.get("allow_private", ()),
                            allow_tcp=add_options.get("allow_tcp", ()))
    if not plan_only and not shutil.which("podman"):
        raise ValueError(f"{found['name']}: Podman is not there to build it (sandbox-ai doctor)")
    data = archive_of(found, run=run, http=http, out=out)
    if source_sha256 is not None and source_sha256 != found["archive_sha256"]:
        raise ValueError(f"{found['name']}: the source archive's SHA-256 is {found['archive_sha256']}, not "
                         f"{source_sha256} that was asked about: nothing is built (ask again)")
    if plan_only:
        return {**plan_json(found), "question": question(found)}
    asked = question(found)
    if confirm is None or not confirm(asked):
        m.LOG.info("mcp", f"build of {found['entry']} not confirmed: nothing built")
        raise ValueError(f"{found['name']} is not built: not confirmed. {found['entry']} has no signed image yet; "
                         f"sandbox-ai mcp add {found['name']} --build builds it here from its reviewed source "
                         "(asked first; added unsigned)")
    folder = log_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = folder / f"{found['name']}-{found['commit'][:12]}-{stamp}.log"
    log.touch(mode=0o600)
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({**plan_json(found), "question": asked, "confirmed": True}, indent=1) + "\n")
    work = Path(tempfile.mkdtemp(prefix=f"sandbox-ai-mcp-build-{found['name']}-"))
    tag = None
    try:
        taken = extract(data, found, work)
        try:
            ref, tag = build_image(found, taken["server"], log, out, popen, run)
        except ValueError as exc:
            if isinstance(exc, BuildFailed):
                raise
            raise BuildFailed(f"{found['name']} is not added: {exc}", log) from None
        smoke(found, taken["server"], taken["fixtures"], ref, log, out)
        record = {"commit": found["commit"], "repository": found["repository"], "path": found["path"],
                  "archive_sha256": found["archive_sha256"],
                  "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        definition = m.add(found["entry"], catalogs=[(found["prefix"], found["catalog"])], fetch=fetch, pull=False,
                           unsigned=unsigned, out=out, built={"image": found["image"], "digest": ref.rpartition("@")[2],
                                                              "record": record},
                           **{key: value for key, value in add_options.items() if key != "backend"})
        tag = None  # (kept: the server runs it)
        m.LOG.info("mcp", f"{local} built from {found['repository']} at {found['commit']} and added unsigned ({ref})")
        return definition
    except BuildFailed as exc:
        m.LOG.warning("mcp", f"build of {found['entry']} failed: {str(exc).splitlines()[0]}")
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if tag is not None:
            remove_image(tag, run)


def pending_entry(entries, name):
    """(prefix, entry) of the catalog entry NAME of ENTRIES (catalog_entries') that has no image yet (pending) and is
    in exactly one catalog, else None."""
    with contextlib.suppress(ValueError):
        entry = m.find_entry(entries, name)
        if not (isinstance(entry.get("digest"), str) and entry["digest"]) and entry.get("kind") in (None, "oci") \
                and (entry.get("_signature") or {}).get("state") != "bad":
            return next((p, e) for p, e in entries if e is entry)
    return None
