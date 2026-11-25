#!/usr/bin/env python3
"""Toolchains built on this machine: a signed recipe instead of a signed archive.

Some build tools may not be passed on. The Android SDK is published under the Android Software Development Kit
License Agreement, whose section 3.4 forbids copying and redistributing the SDK or any part of it: nobody but Google
may offer an archive of it, so the signed catalog (toolchains.json of sandbox-ai-mcp) cannot hold one. It holds a
recipe instead - an entry of its list "recipes" with the manifest every toolchain has and, in place of the archive,

  "recipe": {"terms": "android-sdk-license",          the license you accept before anything is fetched (TERMS)
             "vars": {"license": "..."},              texts the steps name as {license}
             "steps": [STEP, ...]}
  "sha256": the SHA-256 of the recipe (digest): what its tree is called in the store, as an archive's is

and the steps are data, never a shell:

  {"fetch": FILE, "url": "https://...", "size": BYTES, "sha256": HEX, "sha1": HEX}
      a download from its publisher, used only with exactly that size and those hashes (one of them at least)
  {"unpack": FILE, "to": DIR, "strip": N}     the zip FILE into DIR of the tree, without trusting it
  {"write": PATH, "text": TEXT, "mode": "644"}   a small file (the license's acceptance, a package.xml)
  {"link": PATH, "to": TARGET}                a relative symbolic link inside the tree
  {"chmod": PATH, "mode": "755"}

In a text "{prefix}" is /opt/toolchains/NAME, "{version}" and "{arch}" the entry's, "{KEY}" a value of "vars".

What is signed and published is the recipe alone: URLs, sizes, hashes, the small files. `sandbox-ai toolchain add`
checks the catalog's signature, says the license and asks (--accept-android-sdk-license answers for a script; the
security modes high and paranoid never ask: the option or nothing), then fetches each file from its publisher,
checks it and builds the tree in the store of this machine, read-only like every other toolchain's. Nothing of it
is ever uploaded or passed on by sandbox-ai.

Standard library only.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import zipfile

MOUNT = "/opt/toolchains"
VERBS = ("fetch", "unpack", "write", "link", "chmod")
FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")
PLACE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
SHA256 = re.compile(r"[0-9a-f]{64}")
SHA1 = re.compile(r"[0-9a-f]{40}")
STEPS_LIMIT = 256
VARS_LIMIT = 16
VAR_BYTES = 64 << 10
WRITE_BYTES = 256 << 10
DOWNLOAD_LIMIT = 4 << 30           # bytes of one download
UNPACKED_LIMIT = 16 << 30          # bytes of what a recipe unpacks to
MEMBERS_LIMIT = 500000             # entries of one archive
BUILT_IN = ("prefix", "version", "arch")

# The licenses a recipe may ask you to accept. A recipe that names another is refused (a newer sandbox-ai knows it):
# what you accept is said by sandbox-ai itself, never by a catalog's text alone.
TERMS = {
    "android-sdk-license": {
        "title": "Android Software Development Kit License Agreement",
        "by": "Google",
        "url": "https://developer.android.com/studio/terms",
        "option": "--accept-android-sdk-license",
        "says": ("it lets you use the SDK only to develop apps for compatible implementations of Android, and forbids "
                 "copying, changing and redistributing the SDK or any part of it (section 3.4)"),
    },
}
# The toolchains and service images known to need a license, before any catalog is read: what a start offers for
# one that is missing says the license then (the catalog's recipe decides at `toolchain add`).
LICENSED = {"android-sdk": "android-sdk-license", "android-emulator": "android-sdk-license"}


def shown(value, limit=300):
    text = str(value)[:limit]
    return "".join(c if " " <= c <= "~" else c.encode("unicode_escape").decode("ascii") for c in text)


# --- the recipe: checked before anything of it is used ------------------------------------------------------------


def below(value, what="a path below the toolchain's prefix", dot=False):
    """VALUE (a path of a step) as a path below the tree, or ValueError; DOT: "." (the tree itself) is one."""
    if dot and value == ".":
        return "."
    path = PurePosixPath(str(value))
    if not isinstance(value, str) or not value or path.is_absolute() or len(value) > 512 or \
            any(part in ("", ".", "..") for part in path.parts) or not re.fullmatch(r"[A-Za-z0-9._+@/-]+", value):
        raise ValueError(f"{shown(value)!r} is not {what}")
    return path.as_posix()


def url_checked(url, where):
    if not isinstance(url, str) or len(url) > 2048 or any(c <= " " or c > "~" for c in url) or \
            not (url.startswith("https://") or url.startswith("/") or url.startswith("file:///")):
        raise ValueError(f"{where}: url must be an https URL (or a local file of a catalog of your own)")
    return url


def validated(recipe, where="recipe"):
    """The RECIPE with every value checked (a new dict: {"terms", "vars", "steps"}), or ValueError. Its steps become
    downloads and files of this machine: nothing of it is used before."""
    if not isinstance(recipe, dict) or set(recipe) - {"terms", "vars", "steps"}:
        raise ValueError(f"{where}: a recipe is {{terms, vars, steps}}")
    terms = recipe.get("terms")
    if terms is not None and terms not in TERMS:
        raise ValueError(f"{where}: it asks you to accept the license {shown(terms)!r}, which this sandbox-ai does "
                         "not know (sandbox-ai update)")
    variables = recipe.get("vars", {})
    if not isinstance(variables, dict) or len(variables) > VARS_LIMIT or not all(
            isinstance(key, str) and KEY.fullmatch(key) and key not in BUILT_IN and isinstance(value, str)
            and len(value.encode()) <= VAR_BYTES and "\0" not in value for key, value in variables.items()):
        raise ValueError(f"{where}: vars must map lower-case names to texts (not {', '.join(BUILT_IN)})")
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not steps or len(steps) > STEPS_LIMIT:
        raise ValueError(f"{where}: steps must be a list of 1 to {STEPS_LIMIT} steps")
    known = {*BUILT_IN, *variables}

    def text(value, at, limit=2048):
        if not isinstance(value, str) or len(value.encode()) > limit or "\0" in value:
            raise ValueError(f"{at}: a text of at most {limit} bytes")
        unknown = [name for name in PLACE.findall(value) if name not in known]
        if unknown:
            raise ValueError(f"{at}: {{{unknown[0]}}} is no variable of the recipe (vars)")
        return value

    checked, fetched, unpacked = [], {}, set()
    for index, step in enumerate(steps, 1):
        at = f"{where}: step {index}"
        verbs = [verb for verb in VERBS if isinstance(step, dict) and verb in step]
        if len(verbs) != 1:
            raise ValueError(f"{at}: exactly one of {', '.join(VERBS)}")
        verb = verbs[0]
        if verb == "fetch":
            if set(step) - {"fetch", "url", "size", "sha256", "sha1"}:
                raise ValueError(f"{at}: fetch is {{fetch, url, size, sha256, sha1}}")
            name = step["fetch"]
            if not isinstance(name, str) or not FILE.fullmatch(name) or name in fetched:
                raise ValueError(f"{at}: fetch names a file (letters, digits, '.', '_', '+', '-'), each once")
            size = step.get("size")
            if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= DOWNLOAD_LIMIT:
                raise ValueError(f"{at}: size must be the download's bytes (at most {DOWNLOAD_LIMIT >> 30} GiB)")
            hashes = {key: step[key] for key in ("sha256", "sha1") if key in step}
            if not hashes or not all(isinstance(value, str) and (SHA256 if key == "sha256" else SHA1).fullmatch(value)
                                     for key, value in hashes.items()):
                raise ValueError(f"{at}: {name} needs its sha256 (64 hex digits) or sha1 (40), or both")
            item = {"fetch": name, "url": url_checked(text(step.get("url"), at), at), "size": size, **hashes}
            fetched[name] = item
        elif verb == "unpack":
            if set(step) - {"unpack", "to", "strip"}:
                raise ValueError(f"{at}: unpack is {{unpack, to, strip}}")
            name = step["unpack"]
            if name not in fetched or name in unpacked:
                raise ValueError(f"{at}: {shown(name)!r} is unpacked once, after its fetch")
            unpacked.add(name)
            strip = step.get("strip", 0)
            if isinstance(strip, bool) or not isinstance(strip, int) or not 0 <= strip <= 8:
                raise ValueError(f"{at}: strip is 0 to 8")
            item = {"unpack": name, "to": text(step.get("to", "."), at, 512), "strip": strip}
        elif verb == "write":
            if set(step) - {"write", "text", "mode"} or step.get("mode", "644") not in ("644", "755"):
                raise ValueError(f"{at}: write is {{write, text, mode: 644 or 755}}")
            item = {"write": text(step["write"], at, 512), "text": text(step.get("text"), at, WRITE_BYTES),
                    "mode": step.get("mode", "644")}
        elif verb == "link":
            if set(step) - {"link", "to"}:
                raise ValueError(f"{at}: link is {{link, to}}")
            item = {"link": text(step["link"], at, 512), "to": text(step.get("to"), at, 512)}
        else:
            if set(step) - {"chmod", "mode"} or step.get("mode") not in ("644", "755"):
                raise ValueError(f"{at}: chmod is {{chmod, mode: 644 or 755}}")
            item = {"chmod": text(step["chmod"], at, 512), "mode": step["mode"]}
        checked.append(item)
    unused = sorted(set(fetched) - unpacked)
    if unused:
        raise ValueError(f"{where}: fetched and never unpacked: {', '.join(unused)}")
    return {"terms": terms, "vars": dict(sorted(variables.items())), "steps": checked}


def digest(name, version, arch, recipe):
    """The SHA-256 of the recipe of NAME VERSION for ARCH: what its tree is called in the store. One changed step,
    URL, hash or text is another recipe and another tree."""
    document = json.dumps({"name": name, "version": version, "arch": arch, "recipe": recipe}, sort_keys=True,
                          separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(document.encode("ascii")).hexdigest()


def values_of(entry):
    return {"prefix": f"{MOUNT}/{entry['name']}", "version": entry["version"], "arch": entry["arch"],
            **entry["recipe"]["vars"]}


def substituted(text, values):
    return PLACE.sub(lambda match: values[match[1]], text)


def downloads(entry):
    """[(url, bytes)] of what the recipe of ENTRY fetches."""
    values = values_of(entry)
    return [(substituted(step["url"], values), step["size"]) for step in entry["recipe"]["steps"] if "fetch" in step]


# --- the license ---------------------------------------------------------------------------------------------------


def terms_of(entry):
    """{"id", "title", "by", "url", "option", "says"} of the license ENTRY's recipe asks you to accept, or None."""
    ident = (entry.get("recipe") or {}).get("terms")
    return {"id": ident, **TERMS[ident]} if ident in TERMS else None


def terms_text(terms, what):
    """What is said before WHAT (the toolchain android-sdk 37.0.0) is fetched under TERMS."""
    return (f"{what} is published by {terms['by']} under the {terms['title']} ({terms['url']}): {terms['says']}. "
            f"sandbox-ai passes nothing of it on: its files are fetched from {terms['by']} by this machine, checked "
            "against the signed recipe and kept here. By going on you accept that agreement")


def on_terminal():
    try:
        return sys.stdin is not None and sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def accept(terms, what, command, accepted=False, mode=None, terminal=None, read=None, out=print):
    """How the license TERMS was accepted for WHAT: "option" (ACCEPTED: its --accept-... option was given) or "asked"
    (the question on this terminal, answered yes). ValueError otherwise, naming COMMAND with the option: without a
    terminal, after a no, and in the security modes high and paranoid, which never ask (MODE)."""
    out(terms_text(terms, what) + ".")
    if accepted:
        out(f"accepted with {terms['option']}")
        return "option"
    how = f"{command} {terms['option']}"
    if mode in ("high", "paranoid"):
        raise ValueError(f"not added: the security mode {mode} asks no question about a license; when you accept it: "
                         f"{how}")
    terminal = on_terminal() if terminal is None else terminal
    if not terminal:
        raise ValueError(f"not added: the license is yours to accept, and there is no terminal to ask on; when you "
                         f"accept it: {how}")
    prompt = f"Do you accept the {terms['title']}? [y/N] "
    try:
        if read is not None:
            reply = read(prompt)
        else:
            print(prompt, end="", file=sys.stderr, flush=True)
            reply = input()
    except EOFError:
        reply = ""
    if reply.strip().lower() not in ("y", "yes", "j", "ja"):
        raise ValueError(f"not added: the license was not accepted (later: {how})")
    return "asked"


# --- a download ----------------------------------------------------------------------------------------------------


def fetch(step, url, target, opener=None, progress=None):
    """The download of STEP from URL written to the file TARGET while it is measured and hashed (never held in
    memory). ValueError - and no file - unless it has exactly the step's size and hashes."""
    hashes = {key: hashlib.new(key) for key in ("sha256", "sha1") if key in step}
    size = 0
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
        raise ValueError(f"{shown(url)}: a download comes from an https URL or a local file")
    try:
        with source, open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600),
                          "wb") as stream:
            while True:
                chunk = source.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > step["size"]:
                    raise ValueError(f"{url}: more than the {step['size']} bytes the recipe names: not used")
                for found in hashes.values():
                    found.update(chunk)
                stream.write(chunk)
                if progress:
                    progress(size)
        if size != step["size"]:
            raise ValueError(f"{url}: {size} bytes, the recipe says {step['size']}: not used")
        for key, found in hashes.items():
            if found.hexdigest() != step[key]:
                raise ValueError(f"{url}: {key.upper().replace('SHA', 'SHA-')} {found.hexdigest()}, the recipe says "
                                 f"{step[key]}: not used")
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(target)
        raise
    return size


# --- unpacking without trusting the archive ------------------------------------------------------------------------


def member_parts(name, strip=0):
    """NAME of an archive member as path parts below the target (after STRIP leading parts), or None: nothing left."""
    parts = [part for part in PurePosixPath(str(name).replace("\\", "/").lstrip("/")).parts if part not in ("", ".")]
    if any(part == ".." or "\0" in part for part in parts):
        raise ValueError(f"archive member {shown(name)!r} leaves the tree")
    return parts[strip:] or None


def folder_for(top, parts):
    """TOP/PARTS with every folder on the way a real folder below TOP (never through a link the archive made)."""
    current = top
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink() or (os.path.lexists(current) and not current.is_dir()):
            raise ValueError(f"archive member {shown('/'.join(parts))} lies below {shown(current.relative_to(top))}, "
                             "which is no folder")
        if not current.is_dir():
            current.mkdir(mode=0o755)
    return current / parts[-1]


def link_inside(top, path, target):
    """A symbolic link PATH -> TARGET; refused when TARGET is absolute or leads out of TOP."""
    if not target or target.startswith("/") or "\0" in target or len(target) > 1024:
        raise ValueError(f"{shown(path.relative_to(top))}: an absolute or empty link ({shown(target)}) is not taken")
    resolved = os.path.normpath(os.path.join(os.path.dirname(path.relative_to(top).as_posix()), target))
    if resolved == ".." or resolved.startswith("../"):
        raise ValueError(f"{shown(path.relative_to(top))}: its link target {shown(target)} leaves the tree")
    if os.path.lexists(path):
        if path.is_dir() and not path.is_symlink():
            raise ValueError(f"{shown(path.relative_to(top))}: a link where a folder is")
        path.unlink()
    os.symlink(target, path)


def unzip(archive, top, strip=0, budget=None, folded=None):
    """The zip ARCHIVE into the folder TOP, without trusting it: no path that leaves it, nothing written through a
    link, no set-user-ID, set-group-ID or world-writable bit, no device. BUDGET ([bytes left]): what the whole recipe
    may still unpack. FOLDED ({casefolded path: path}; None: the file system tells Name from name): two names it
    would take for one are refused."""
    budget = [UNPACKED_LIMIT] if budget is None else budget
    if not zipfile.is_zipfile(archive):
        raise ValueError(f"{archive.name} is no zip archive")
    top.mkdir(mode=0o755, parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > MEMBERS_LIMIT:
            raise ValueError(f"{archive.name} has more than {MEMBERS_LIMIT} entries")
        for info in members:
            parts = member_parts(info.filename, strip)
            if parts is None:
                continue
            if folded is not None:
                relative = (top / Path(*parts)).as_posix()
                other = folded.setdefault(relative.casefold(), relative)
                if other != relative:
                    raise ValueError(f"the archive has {shown(other)} and {shown(relative)}, which this file system "
                                     "takes for one name: not unpacked here")
            mode = (info.external_attr >> 16) & 0o7777
            kind = (info.external_attr >> 16) & 0o170000
            path = folder_for(top, parts)
            if info.is_dir():
                if path.is_symlink() or (os.path.lexists(path) and not path.is_dir()):
                    raise ValueError(f"archive member {shown(info.filename)}: a folder where something else is")
                if not path.is_dir():
                    path.mkdir(mode=0o755)
            elif kind == stat.S_IFLNK:
                if info.file_size > 1024:
                    raise ValueError(f"archive member {shown(info.filename)}: a link target of {info.file_size} bytes")
                link_inside(top, path, source.read(info).decode("utf-8", "replace"))
            elif kind in (0, stat.S_IFREG):
                budget[0] -= info.file_size
                if budget[0] < 0:
                    raise ValueError(f"the recipe unpacks to more than {UNPACKED_LIMIT >> 30} GiB")
                if os.path.lexists(path):
                    if path.is_dir() and not path.is_symlink():
                        raise ValueError(f"archive member {shown(info.filename)}: a file where a folder is")
                    path.unlink()
                left = info.file_size
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                with os.fdopen(fd, "wb") as out, source.open(info) as stream:
                    while True:  # (never more than the entry says it holds: a zip that lies about its size)
                        chunk = stream.read(min(1 << 20, left + 1))
                        if not chunk:
                            break
                        left -= len(chunk)
                        if left < 0:
                            raise ValueError(f"archive member {shown(info.filename)} is larger than it says")
                        out.write(chunk)
                os.chmod(path, 0o755 if mode & 0o111 else 0o644)
            # devices, FIFOs, sockets: left out


def remove(path):
    """A tree (its folders read-only or not), removed."""
    if not os.path.lexists(path):
        return
    for directory, dirs, _ in os.walk(path):
        for name in dirs:
            child = Path(directory) / name
            if not child.is_symlink():
                os.chmod(child, 0o700)
    os.chmod(path, 0o700)
    shutil.rmtree(path)


def inside(tree, value, dot=False):
    relative = below(value, dot=dot)
    return tree if relative == "." else tree.joinpath(*PurePosixPath(relative).parts)


def placed(tree, path):
    """PATH with its folder made, every part of it a real folder of TREE (a step never writes through a link)."""
    return folder_for(tree, path.relative_to(tree).parts)


# --- the build -----------------------------------------------------------------------------------------------------


def build(entry, target, out=print, opener=None, case_sensitive=True):
    """The tree of ENTRY (validated: its "recipe") made at TARGET (new), read-only: each step in its order, every
    download checked before it is unpacked and removed right after. Whatever fails, nothing is left of it."""
    target = Path(target)
    if os.path.lexists(target):
        raise ValueError(f"{target} exists")
    recipe, values = entry["recipe"], values_of(entry)
    work = Path(tempfile.mkdtemp(prefix=".build-", dir=target.parent))
    tree, files, budget = work / "tree", {}, [UNPACKED_LIMIT]
    folded = None if case_sensitive else {}
    fetches = [step for step in recipe["steps"] if "fetch" in step]
    try:
        tree.mkdir(mode=0o755)
        for step in recipe["steps"]:
            if "fetch" in step:
                url = substituted(step["url"], values)
                number = fetches.index(step) + 1
                out(f"sandbox-ai toolchain: fetching {number}/{len(fetches)}: {shown(url)} "
                    f"({max(1, step['size'] >> 20)} MiB)")
                files[step["fetch"]] = work / f"download-{number}"
                fetch(step, url, files[step["fetch"]], opener)
            elif "unpack" in step:
                archive = files.pop(step["unpack"])
                unzip(archive, inside(tree, substituted(step["to"], values), dot=True), step["strip"], budget, folded)
                archive.unlink()
            elif "write" in step:
                path = placed(tree, inside(tree, substituted(step["write"], values)))
                if os.path.lexists(path):
                    if path.is_dir() and not path.is_symlink():
                        raise ValueError(f"{shown(step['write'])}: a file where a folder is")
                    path.unlink()
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(substituted(step["text"], values))
                os.chmod(path, 0o755 if step["mode"] == "755" else 0o644)
            elif "link" in step:
                path = placed(tree, inside(tree, substituted(step["link"], values)))
                link_inside(tree, path, substituted(step["to"], values))
            else:
                path = inside(tree, substituted(step["chmod"], values))
                if path.is_symlink() or not path.is_file() or any(
                        parent.is_symlink() for parent in path.parents if tree in parent.parents):
                    raise ValueError(f"{shown(step['chmod'])}: no file of the tree")
                os.chmod(path, 0o755 if step["mode"] == "755" else 0o644)
        size = 0
        for directory, _, names in os.walk(tree):  # read-only from now on, as every toolchain's tree
            for name in names:
                path = Path(directory) / name
                if not path.is_symlink():
                    info = os.lstat(path)
                    size += info.st_size
                    os.chmod(path, info.st_mode & 0o555)
        os.replace(tree, target)
        for directory, dirs, _ in os.walk(target):
            for name in dirs:
                path = Path(directory) / name
                if not path.is_symlink():
                    os.chmod(path, 0o555)
        os.chmod(target, 0o555)
    except BaseException:
        remove(target)
        raise
    finally:
        remove(work)
    return size
