#!/usr/bin/env python3
"""Your own images - from a registry (Docker Hub, quay.io, ghcr.io, any registry) or built from your own
Containerfile - and services: containers of them that run beside the agent, in its sandbox.

Your images (sandbox-ai images pull|build-image|list|remove):

  Each is recorded in ~/.config/sandbox-ai/user-images.json, pinned when it is added: an image from a registry by
  its digest (repository@sha256:...), pulled once, then and only then (every start runs with --pull=never); an image
  of your own Containerfile by its image ID, with every FROM (and COPY --from) image pulled once and pinned by digest
  in the Containerfile that is built. sandbox-ai has not reviewed any of them: each is marked unchecked, wherever it
  is shown. Its age does not matter.

A service (--service NAME[:KEY=VALUE,...], repeatable; also in a project file's options and on the board):

  runs one of your images as a container in the sandbox's Podman pod, started before the agent and removed with the
  run. It shares the sandbox's network namespace: the agent reaches it at localhost:PORT, and whatever it sends meets
  the same firewall, connection proxy, allow-list, approvals, kill switch (sandbox-ai net cut) and VPN as the agent's
  own connections - it gets nothing on the network the agent does not get. It is started only once the sandbox's
  supervisor has set up the firewall and the proxy (it waits for them, see runtime.await_services), with no
  capabilities, no new privileges, a numeric user that is none of the IDs the firewall lets past the proxy or Tor, a
  read-only root, its data in a tmpfs (or a Podman volume kept with the start), the sandbox's resolver and hosts
  file, and limits of its own (memory, CPUs, processes). It is created and initialised first, and started only once
  it is proven to be in the pod's user namespace (and, with --vpn, in the VPN's network namespace with its kill
  switch). Like the agent's container it shares the pod's IPC namespace (System V and POSIX shared memory, message
  queues) and, with the network namespace, the abstract Unix sockets of the agent's sandbox: no more than the agent
  could reach itself, but use only an image you trust beside it. Keys of NAME[:KEY=VALUE,...] (no commas in values):

    image=IMAGE        your image (sandbox-ai images list); default: NAME
    port=PORT          a TCP port it listens on (repeatable); the agent reaches it at localhost:PORT
    ready=PORT|none    the agent starts once PORT accepts connections (default: the first port)
    ready-timeout=S    how long to wait for it, in seconds (default 60)
    env=KEY=VALUE      a variable (shown on the board and in the project file: nothing secret); env=KEY passes
                       the variable KEY of your environment (its value never shown)
    secret=KEY[=NAME]  KEY from the secret NAME of sandbox-ai secrets (default: KEY); the value never shown
    data=PATH          a writable folder (repeatable): a tmpfs that goes with the run, or with keep=yes a Podman
                       volume kept as long as the start is kept
    keep=yes|no        keep the data folders with the start (default no)
    user=UID[:GID]     the user it runs as (default: the image's own, read from it when it was added)
    read-only=yes|no   a read-only root file system (default yes)
    memory=SIZE cpus=N pids=N   its limits (default 512m, 1, 256)

Standard library only.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

LOADING = threading.RLock()  # the lazily loaded helper modules below
HERE = Path(__file__).resolve().parent

# -- your images --

NAME = r"[a-z0-9][a-z0-9_.-]{0,29}"  # as the variants' names: one list of names for both (images.valid_name)
REGISTRY_NAME = "user-images.json"
REGISTRY_LIMIT = 1 << 20
CONTAINERFILE_LIMIT = 1 << 20
IMAGE_TAG = "localhost/sandbox-ai-image-{name}:latest"  # an image of your own Containerfile
USER_IMAGE_LABEL = "sandbox-ai.user-image"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
IMAGE_ID = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
DOMAIN = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
                    r"[A-Za-z0-9])?)*(?::[0-9]{1,5})?")
PATH_PART = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
UNCHECKED = ("unchecked: sandbox-ai has not reviewed this image; it is pinned to what was pulled or built when it "
             "was added, nothing more")
# The agent's layers (Containerfile.AGENT) are built for Debian 12 "bookworm", as the stock image's Node.js base:
# its package names (libasound2, libgtk-3-0 ... renamed in later releases) and Firejail's version.
BASE_DISTRIBUTION = {"ID": "debian", "VERSION_CODENAME": "bookworm"}


def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai"


def registry_path():
    return config_dir() / REGISTRY_NAME


_images = None


def images_module():
    """images.py (its podman command line, location check, history and variants), loaded once."""
    global _images
    if _images is None:
        spec = importlib.util.spec_from_file_location("images_for_services", HERE / "images.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _images is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _images = loaded
    return _images


_launcher = None


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_services", HERE / "agent_sandbox.py")
        with LOADING:
            if _launcher is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _launcher = loaded
    return _launcher


def valid_name(name):
    return isinstance(name, str) and bool(re.fullmatch(NAME, name)) and name not in images_module().RESERVED


def parse_reference(ref):
    """{"domain", "path", "tag", "digest"} of an image reference as Podman and Docker read it: "redis" is
    docker.io/library/redis:latest, "ghcr.io/o/r@sha256:..." keeps its digest. ValueError for anything else."""
    if not isinstance(ref, str) or not ref or len(ref) > 512 or any(c.isspace() or ord(c) < 32 for c in ref):
        raise ValueError(f"not an image reference: {ref!r}")
    name, _, digest = ref.partition("@")
    if digest and not DIGEST.fullmatch(digest):
        raise ValueError(f"{ref}: a digest is sha256: and 64 hexadecimal digits")
    tag = None
    slash = name.rfind("/")
    if ":" in name[slash + 1:]:
        name, _, tag = name.rpartition(":")
        if not TAG.fullmatch(tag):
            raise ValueError(f"{ref}: not a valid tag {tag!r}")
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        domain, parts = parts[0], parts[1:]
        if not DOMAIN.fullmatch(domain):
            raise ValueError(f"{ref}: not a valid registry {domain!r}")
    else:
        domain = "docker.io"
    if domain in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        domain = "docker.io"
        if len(parts) == 1:
            parts = ["library", *parts]
    if not parts or not all(PATH_PART.fullmatch(part) for part in parts):
        raise ValueError(f"{ref}: a repository is lower-case letters, digits and . _ - between slashes")
    return {"domain": domain, "path": "/".join(parts), "tag": tag if tag or digest else "latest",
            "digest": digest or None}


def repository(parsed):
    return f"{parsed['domain']}/{parsed['path']}"


def pinned_reference(parsed, digest):
    return f"{repository(parsed)}@{digest}"


def default_name(parsed):
    """A name for an image of PARSED: the repository's last part, made a valid name."""
    base = re.sub(r"[^a-z0-9_.-]", "-", parsed["path"].rsplit("/", 1)[-1].lower()).strip("._-")[:30] or "image"
    return base if valid_name(base) else f"{base[:26]}-img"


def empty_registry():
    return {"version": 1, "images": {}}


def read_registry(path=None):
    """{name: record} of your images; {} without a file. ValueError for a damaged one (it is never rewritten then)."""
    path = Path(path or registry_path())
    if not path.exists() and not path.is_symlink():
        return {}
    data = images_module().read_limited(path, REGISTRY_LIMIT)
    try:
        document = json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"{path}: {exc}") from None
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("images"), dict):
        raise ValueError(f"{path}: expected version 1 with an object \"images\"")
    found = {}
    for name, record in document["images"].items():
        problem = record_problem(name, record)
        if problem:
            raise ValueError(f"{path}: {name!r}: {problem}")
        found[name] = record
    return found


def record_problem(name, record):
    """Why NAME's RECORD cannot be used (a damaged or hand-edited file), else None."""
    if not valid_name(name):
        return "not a valid name"
    if not isinstance(record, dict) or record.get("kind") not in ("pulled", "built"):
        return "kind is pulled or built"
    if not isinstance(record.get("id"), str) or not IMAGE_ID.fullmatch(record["id"]):
        return "no image ID"
    if record["kind"] == "pulled":
        if not isinstance(record.get("image"), str) or not re.fullmatch(r".+@" + DIGEST.pattern, record["image"]):
            return "a pulled image is pinned by digest (repository@sha256:...)"
        try:
            parse_reference(record["image"])
        except ValueError as exc:
            return str(exc)
    elif record.get("image") != IMAGE_TAG.format(name=name):
        return f"a built image is {IMAGE_TAG.format(name=name)}"
    user = record.get("user")
    if user is not None and not (isinstance(user, str) and re.fullmatch(r"[0-9]{1,10}(?::[0-9]{1,10})?", user)):
        return "user is UID[:GID]"
    ports = record.get("ports", [])
    if not isinstance(ports, list) or not all(type(p) is int and 0 < p < 65536 for p in ports):
        return "ports is a list of TCP ports"
    if not isinstance(record.get("os", {}), dict):
        return "os is an object"
    return None


def write_registry(found):
    images = images_module()
    images.check_location(config_dir(), "Your images' records in")
    document = {"version": 1, "images": {name: found[name] for name in sorted(found)}}
    images.write_private(registry_path(), (json.dumps(document, indent=2) + "\n").encode())


def record(name, found=None):
    """The record of your image NAME, else ValueError naming what to do."""
    found = read_registry() if found is None else found
    if name not in found:
        raise ValueError(f"no image {name} of yours; add it with: sandbox-ai images pull REFERENCE --as {name} (or "
                         f"sandbox-ai images build-image {name} --file Containerfile); see sandbox-ai images list")
    return found[name]


def run_podman(argv, run=None, capture=True, timeout=600, env=None):
    run = run or subprocess.run
    kwargs = {"capture_output": True, "text": True} if capture else {}
    return run(argv, stdin=subprocess.DEVNULL, timeout=timeout, **kwargs, **({"env": env} if env else {}))


def inspect_image(image, podman, run=None):
    """The inspect document of IMAGE (a reference or an ID), or None."""
    result = run_podman([*podman, "image", "inspect", "--format", "json", image], run, timeout=60)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)[0]
    except (ValueError, IndexError, TypeError):
        return None
    return data if isinstance(data, dict) else None


TAR_SLACK = 64 << 10  # what a tar stream of one file holds beyond the file: its headers, padding and end blocks
_THROUGH_RUN = object()


def copy_bounded(argv, cap):
    """The standard output of ARGV (podman cp CONTAINER:PATH -), read as a stream: None once it holds more than CAP
    bytes (the copy is killed then: nothing of an image decides how much this process reads), or when it fails."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    chunks, total = [], 0
    try:
        while True:
            chunk = process.stdout.read(min(1 << 16, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > cap:
                return None
        return b"".join(chunks) if process.wait(timeout=120) == 0 else None
    finally:
        if process.poll() is None:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)


def image_file(image, path, podman, run=None, limit=1 << 20, copy=_THROUGH_RUN):
    """The text of PATH in IMAGE, read without running anything of the image: a container is created (never
    started), the file copied out of it as a tar stream, the container removed. None when it cannot be read - and
    for anything but one regular file of at most LIMIT bytes (a directory, a device, a FIFO, a link to another file,
    more than one member; /etc/os-release may be a symbolic link to /usr/lib/os-release). The stream is read only up
    to LIMIT and a tar header's slack (copy_bounded). RUN: podman's runner (tests); the copy goes through it too
    unless COPY is None (then, as without RUN, it streams through copy_bounded)."""
    import tarfile
    cap = limit + TAR_SLACK
    try:
        created = run_podman([*podman, "create", "--pull=never", "--network=none", "--entrypoint=/nonexistent", image],
                             run, timeout=120)
        made = created.stdout.decode("utf-8", "replace") if isinstance(created.stdout, bytes) else \
            str(created.stdout or "")
        if created.returncode != 0 or not made.strip():
            return None
        container = made.strip().splitlines()[-1]
        argv = [*podman, "cp", f"{container}:{path}", "-"]
        try:
            if run is not None and copy is _THROUGH_RUN:
                copied = run(argv, capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
                data = copied.stdout if isinstance(copied.stdout, bytes) else str(copied.stdout or "").encode()
                data = data if copied.returncode == 0 and len(data) <= cap else None
            else:
                data = copy_bounded(argv, cap)
        finally:
            run_podman([*podman, "rm", "--force", container], run, timeout=120)
        if data is None:
            return None
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            members = archive.getmembers()
            if len(members) != 1:
                return None  # a directory (its entries follow), or more than the one file asked for
            member = members[0]
            if member.issym():  # /etc/os-release -> ../usr/lib/os-release
                return image_file(image, "/usr/lib/os-release", podman, run, limit, copy) \
                    if path == "/etc/os-release" else None
            if member.isreg() and 0 <= member.size <= limit:
                return archive.extractfile(member).read(limit + 1)[:limit].decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError, tarfile.TarError, IndexError, ValueError):
        return None
    return None


def os_release(text):
    """{KEY: value} of an os-release file."""
    found = {}
    for line in (text or "").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and re.fullmatch(r"[A-Z_][A-Z0-9_]{0,40}", key):
            try:
                words = shlex.split(value)
            except ValueError:
                continue
            found[key] = words[0][:200] if words else ""
    return {key: found[key] for key in ("ID", "ID_LIKE", "VERSION_ID", "VERSION_CODENAME", "PRETTY_NAME")
            if key in found}


def passwd_entry(text, user):
    """(uid, gid) of USER in the passwd TEXT (the first match, as the engine reads it), or None."""
    for line in (text or "").splitlines():
        fields = line.strip().split(":")
        if len(fields) >= 4 and fields[0].strip() == user:
            try:
                return int(fields[2].strip()), int(fields[3].strip())
            except ValueError:
                return None
    return None


def numeric_user(config_user, passwd):
    """The image's configured user as "UID:GID" (the engine would resolve a name in the image's /etc/passwd; a
    service runs with the number, so that the firewall's checks hold for what it really is), "0:0" for none or root,
    None when a name cannot be resolved."""
    user, _, group = str(config_user or "").strip().partition(":")
    if not user or user == "root":
        return "0:0" if not group or group in ("0", "root") else None
    if re.fullmatch(r"[0-9]{1,10}", user):
        uid, gid = int(user), None
    else:
        entry = passwd_entry(passwd, user)
        if entry is None:
            return None
        uid, gid = entry
    if group:
        if not re.fullmatch(r"[0-9]{1,10}", group):
            return None  # a group name: not resolved here
        gid = int(group)
    return f"{uid}:{gid if gid is not None else uid}"


def exposed_ports(data):
    ports = []
    for key in ((data.get("Config") or {}).get("ExposedPorts") or {}):
        number, _, proto = str(key).partition("/")
        if number.isdigit() and proto in ("", "tcp") and 0 < int(number) < 65536:
            ports.append(int(number))
    return sorted(set(ports))[:32]


def describe_image(data, podman, image, run=None):
    """What a record keeps of an image: its ID, user, ports, platform, operating system."""
    config = data.get("Config") or {}
    passwd = image_file(image, "/etc/passwd", podman, run)
    release = os_release(image_file(image, "/etc/os-release", podman, run))
    loader = sorted({entry.partition("=")[0] for entry in config.get("Env") or []
                     if entry.partition("=")[0] in images_module().LOADER_ENV})
    return {"id": str(data.get("Id") or ""), "created": str(data.get("Created") or "")[:19],
            "size": int(data.get("Size") or 0), "config_user": str(config.get("User") or ""),
            "user": numeric_user(config.get("User"), passwd), "ports": exposed_ports(data),
            "platform": f"{data.get('Os') or '?'}/{data.get('Architecture') or '?'}", "os": release,
            **({"loader_env": loader} if loader else {})}


def name_taken(name, found, replace):
    if not valid_name(name):
        raise ValueError(f"{name!r}: a name is lower-case letters, digits, '.', '_' and '-' (up to 30 characters), and "
                         f"not one of {', '.join(sorted(images_module().RESERVED))}")
    if any(variant == name for variant, _ in images_module().variants()):
        raise ValueError(f"{name} is the name of an image variant (sandbox-ai images new); choose another one")
    if name in found and not replace:
        raise ValueError(f"you have an image {name} already ({found[name].get('ref') or found[name]['image']}); "
                         "--replace pins it anew, or choose another name with --as NAME")


def warning(name, entry):
    where = entry.get("ref") or entry.get("containerfile") or entry["image"]
    return (f"WARNING: {name} ({where}) is UNCHECKED - {UNCHECKED}. A service of it runs behind the sandbox's "
            "firewall and proxy like the agent; as a variant's base it runs the sandbox's own supervisor (firewall, "
            "proxy, approvals): use only an image you trust there")


def pull(ref, name=None, replace=False, podman=None, run=None, out=print):
    """Pull REF once (explicitly: never at a start) and record it, pinned by its digest. Returns (name, record)."""
    parsed = parse_reference(ref)
    if parsed["domain"] == "localhost":
        raise ValueError(f"{ref}: a local image is no registry's; build yours with sandbox-ai images build-image")
    found = read_registry()
    name = name or default_name(parsed)
    name_taken(name, found, replace)
    podman = podman or images_module().podman()
    wanted = f"{repository(parsed)}@{parsed['digest']}" if parsed["digest"] else f"{repository(parsed)}:{parsed['tag']}"
    out(f"sandbox-ai images: pulling {wanted} (once: starts never pull)")
    pulled = run_podman([*podman, "pull", wanted], run, capture=False, timeout=3600)
    if pulled.returncode != 0:
        raise ValueError(f"podman pull {wanted} failed (exit status {pulled.returncode})")
    data = inspect_image(wanted, podman, run)
    if data is None:
        raise ValueError(f"{wanted} was pulled but cannot be inspected")
    digest = parsed["digest"] or repo_digest(data, repository(parsed))
    if digest is None:
        raise ValueError(f"{wanted}: Podman names no digest of {repository(parsed)} for it; pull it by digest: "
                         f"sandbox-ai images pull {repository(parsed)}@sha256:...")
    image = pinned_reference(parsed, digest)
    entry = {"kind": "pulled", "ref": ref, "image": image, "digest": digest, "reviewed": False,
             "added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **describe_image(data, podman, image, run)}
    previous = found.get(name)
    found[name] = entry
    write_registry(found)
    images_module().remember("pull", name, ref=ref, image=image, id=entry["id"][:19],
                             **({"replaced": previous.get("image")} if previous else {}))
    out(f"sandbox-ai images: {name} = {image}")
    out(f"sandbox-ai images: {warning(name, entry)}")
    return name, entry


def repo_digest(data, repo):
    """The digest under which REPO names the image of the inspect DATA: its own manifest's ("Digest") when the
    repository lists it, else the repository's first."""
    listed = []
    for value in data.get("RepoDigests") or []:
        try:
            parsed = parse_reference(str(value))
        except ValueError:
            continue
        if parsed["digest"] and repository(parsed) == repo:
            listed.append(parsed["digest"])
    own = str(data.get("Digest") or "")
    if own in listed:
        return own
    return listed[0] if listed else (own if DIGEST.fullmatch(own) else None)


# -- your own Containerfiles --

def external_images(text):
    """[(line, reference)] of the images TEXT (a Containerfile) takes from outside: every FROM that is no stage
    of its own (or scratch), every COPY --from=IMAGE. ValueError for what cannot be pinned (a variable in an image
    reference, RUN --mount=...,from=IMAGE, an escape character other than the backslash)."""
    # Parser directives (# NAME=VALUE before anything else): another escape character makes the engine join lines
    # otherwise than logical_lines does - a FROM after "RUN echo \" would be an instruction of its own, never pinned.
    for number, line in enumerate(text.split("\n"), 1):
        directive = re.fullmatch(r"\s*#\s*([A-Za-z]+)\s*=\s*(.*?)\s*", line)
        if not directive:
            break
        if directive[1].lower() == "escape" and directive[2] != "\\":
            raise ValueError(f"line {number}: # escape={directive[2]}: only the backslash escapes in a Containerfile "
                             "sandbox-ai pins (its lines are read as the engine reads them then)")
    lines = images_module().logical_lines(text)
    found, seen = [], set()  # seen: the stages named so far (FROM a later stage's name is an image)
    for first, _, keyword, arguments in lines:
        words = arguments.split()
        if keyword == "FROM":
            words = [w for w in words if not w.startswith("--platform")]
            if not words:
                raise ValueError(f"line {first}: FROM names no image")
            image = words[0]
            if image.lower() == "scratch" or image.lower() in seen:
                pass
            elif "$" in image:
                raise ValueError(f"line {first}: FROM {image}: a variable cannot be pinned; write the image itself")
            else:
                found.append((first, image))
            if len(words) >= 3 and words[-2].lower() == "as":
                seen.add(words[-1].lower())
        elif keyword in ("COPY", "ADD"):
            for word in words:
                if word.startswith("--from="):
                    source = word[len("--from="):]
                    if source.lower() in seen or source.isdigit():
                        continue
                    if "$" in source:
                        raise ValueError(f"line {first}: {word}: a variable cannot be pinned; write the image itself")
                    found.append((first, source))
        elif keyword == "RUN":
            for word in words:
                if word.startswith("--mount="):
                    for option in word[len("--mount="):].split(","):
                        key, _, value = option.partition("=")
                        if key == "from" and value.lower() not in seen and not value.isdigit():
                            raise ValueError(f"line {first}: RUN --mount from={value}: an image there cannot be "
                                             "pinned; COPY --from it in a stage instead")
    return found


def pin_containerfile(text, pins):
    """TEXT with every external image of external_images replaced by its pinned reference in PINS ({ref: pinned})."""
    lines = text.split("\n")
    for number, ref in external_images(text):
        index = number - 1
        pattern = re.compile(r"(?<![^\s=])" + re.escape(ref) + r"(?=\s|$)")
        # the logical instruction may continue over several lines: the reference is in the first that has it
        for at in range(index, len(lines)):
            if pattern.search(lines[at]):
                lines[at] = pattern.sub(pins[ref], lines[at], count=1)
                break
    return "\n".join(lines)


BUILD_NETWORK_NOTE = ("its RUN steps reach the network from this host directly - outside the sandbox's firewall, VPN "
                      "and Tor, with your own address")


def build(name, file, context=None, replace=False, podman=None, run=None, out=print, trust_agent_written=False,
          network=False):
    """Build your Containerfile FILE (in CONTEXT, default its folder) as your image NAME: each image it takes from
    outside pulled once and pinned by digest in the Containerfile that is built (podman build --pull=never), the
    result recorded by its image ID. A Containerfile or context where a sandbox can write (an agent could have
    changed it) is refused unless TRUST_AGENT_WRITTEN (--trust-agent-written: you reviewed it). Its RUN steps get no
    network (--network=none) unless NETWORK (--build-network: this host's own, outside the sandbox's firewall, VPN
    and Tor). Returns (name, record)."""
    images = images_module()
    found = read_registry()
    name_taken(name, found, replace)
    path = Path(os.path.abspath(os.path.expanduser(str(file))))
    folder = Path(os.path.abspath(os.path.expanduser(str(context)))) if context else path.parent
    if not folder.is_dir():
        raise ValueError(f"--context {folder}: not a folder")
    agent_written = False
    for where, what in ((path, "the Containerfile"), (folder, "the build context")):
        try:
            images.check_location(where, what)
        except ValueError as exc:
            if not trust_agent_written:
                raise ValueError(f"{exc}: an agent could have changed it, and its build runs on this host. Review "
                                 "it (the Containerfile and every file it copies), then build with "
                                 "--trust-agent-written - or copy them to a folder of your own") from None
            agent_written = True
            out(f"sandbox-ai images: {exc}: trusted as reviewed (--trust-agent-written)")
    if network:
        out(f"sandbox-ai images: --build-network: {BUILD_NETWORK_NOTE}")
    text = images.read_limited(path, CONTAINERFILE_LIMIT).decode("utf-8", "replace")
    podman = podman or images.podman()
    pins, sources = {}, []
    for _, ref in external_images(text):
        if ref in pins:
            continue
        parsed = parse_reference(ref)
        wanted = f"{repository(parsed)}@{parsed['digest']}" if parsed["digest"] else \
            f"{repository(parsed)}:{parsed['tag']}"
        if parsed["domain"] == "localhost":
            data = inspect_image(wanted, podman, run)
            if data is None:
                raise ValueError(f"{ref}: no such local image")
            pins[ref] = str(data.get("Id") or "").removeprefix("sha256:")
            sources.append({"ref": ref, "pinned": pins[ref], "local": True})
            continue
        out(f"sandbox-ai images: pulling {wanted} for {path.name} (once, pinned by digest)")
        pulled = run_podman([*podman, "pull", wanted], run, capture=False, timeout=3600)
        if pulled.returncode != 0:
            raise ValueError(f"podman pull {wanted} failed (exit status {pulled.returncode})")
        data = inspect_image(wanted, podman, run)
        digest = parsed["digest"] or (repo_digest(data, repository(parsed)) if data else None)
        if digest is None:
            raise ValueError(f"{wanted}: Podman names no digest for it")
        pins[ref] = pinned_reference(parsed, digest)
        sources.append({"ref": ref, "pinned": pins[ref]})
    pinned_text = pin_containerfile(text, pins)
    tag = IMAGE_TAG.format(name=name)
    with tempfile.TemporaryDirectory(prefix="sandbox-ai-user-image-") as scratch:
        generated = Path(scratch) / "Containerfile"
        generated.write_text(pinned_text)
        argv = [*podman, "build", "--pull=never", *([] if network else ["--network=none"]), "--file", str(generated),
                "--tag", tag,
                "--label", f"{USER_IMAGE_LABEL}={name}", str(folder)]
        built = run_podman(argv, run, capture=False, timeout=7200)
    if built.returncode != 0:
        images.remember("build-image-failed", name, containerfile=str(path))
        raise ValueError(f"podman build of {path} failed (exit status {built.returncode})" + (
            "; its RUN steps had no network: --build-network gives them this host's (outside the sandbox's firewall, "
            "VPN and Tor)" if not network else ""))
    data = inspect_image(tag, podman, run)
    if data is None:
        raise ValueError(f"{tag} was built but cannot be inspected")
    entry = {"kind": "built", "image": tag, "containerfile": str(path), "context": str(folder),
             "containerfile_sha256": hashlib.sha256(text.encode()).hexdigest(), "from": sources, "reviewed": False,
             "build_network": bool(network), "agent_written": agent_written,
             "added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             **describe_image(data, podman, str(data.get("Id") or tag), run)}
    found[name] = entry
    write_registry(found)
    images.remember("build-image", name, containerfile=str(path), id=entry["id"][:19],
                    pinned=[source["pinned"] for source in sources])
    out(f"sandbox-ai images: {name} = {tag} ({entry['id'][:19]})")
    out(f"sandbox-ai images: {warning(name, entry)}")
    return name, entry


def users_of(name):
    """The variants whose base is your image NAME."""
    images, found = images_module(), []
    for variant, _ in images.variants():
        try:
            if images.load(variant, strict=False)[0].get("base") == name:
                found.append(variant)
        except ValueError:
            continue
    return found


def remove(name, keep_image=False, podman=None, run=None):
    """Forget your image NAME (and remove its image unless KEEP_IMAGE). Refused while a variant is based on it."""
    found = read_registry()
    entry = record(name, found)
    using = users_of(name)
    if using:
        raise ValueError(f"{name} is the base of the variant(s) {', '.join(using)}; give them another base or remove "
                         "them first (sandbox-ai images extend VARIANT --no-base, or images remove VARIANT)")
    notes = []
    del found[name]
    write_registry(found)
    if not keep_image:
        podman = podman or images_module().podman()
        result = run_podman([*podman, "image", "rm", entry["image"]], run, timeout=120)
        if result.returncode != 0 and "image not known" not in (result.stderr or ""):
            notes.append(f"the image {entry['image']} stays: {(result.stderr or '').strip()[-300:]}")
    images_module().remember("forget", name, image=entry["image"])
    return notes


def present_ids(podman, run=None):
    """The full IDs of the images in Podman's store, or None when it cannot be asked."""
    try:
        result = run_podman([*podman, "images", "--no-trunc", "--format", "{{.ID}}"], run, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return {line.strip().removeprefix("sha256:") for line in (result.stdout or "").splitlines() if line.strip()}


def base_problem(entry):
    """Why your image ENTRY cannot be a variant's base, else None."""
    release = entry.get("os") or {}
    if entry.get("loader_env"):
        return (f"its configuration sets {', '.join(entry['loader_env'])}, which would load code into the sandbox's "
                "own supervisor")
    if not release:
        return "its operating system is unknown (no /etc/os-release): the agent's layers need Debian 12 (bookworm)"
    if release.get("ID") != BASE_DISTRIBUTION["ID"] or release.get("VERSION_CODENAME") != \
            BASE_DISTRIBUTION["VERSION_CODENAME"]:
        what = release.get("PRETTY_NAME") or f"{release.get('ID', '?')} {release.get('VERSION_ID', '')}".strip()
        return (f"it is {what}; the agent's layers (sandbox-ai's Containerfile: Firejail, nftables, the libraries of "
                "the display) are built for Debian 12 (bookworm), as the stock image: choose the image's bookworm "
                "variant (e.g. python:3.12-bookworm, node:22-bookworm, golang:1.23-bookworm, rust:1-bookworm)")
    if not str(entry.get("platform", "")).startswith("linux/"):
        return f"it is an image for {entry.get('platform')}"
    return None


def listing(podman=None, run=None, check=True):
    """[{name, ...record, present, unchecked, used_by, base_problem}] of your images; PRESENT is None when Podman
    could not be asked (or CHECK is false)."""
    found = read_registry()
    ids = present_ids(podman or images_module().podman(), run) if check and found else None
    rows = []
    for name in sorted(found):
        entry = dict(found[name])
        entry.update(name=name, unchecked=not entry.get("reviewed"), used_by=users_of(name),
                     present=None if ids is None else entry["id"].removeprefix("sha256:") in ids,
                     base_problem=base_problem(entry))
        rows.append(entry)
    return rows


def show_rows(rows, out):
    if not rows:
        out("no images of yours; add one: sandbox-ai images pull REFERENCE [--as NAME], or "
            "sandbox-ai images build-image NAME --file Containerfile")
        return
    for row in rows:
        state = "" if row["present"] is not False else "  MISSING from Podman: add it again (--replace)"
        source = row.get("ref") or row.get("containerfile") or ""
        out(f"{row['name']:18} {'UNCHECKED' if row['unchecked'] else 'reviewed':10} {row['kind']:7} {row['image']}"
            f"{state}")
        details = [f"from {source}" if source and source != row["image"] else "",
                   f"user {row['user']}" if row.get("user") else "user unknown (set user= for a service)",
                   f"ports {','.join(map(str, row['ports']))}" if row.get("ports") else "",
                   (row.get("os") or {}).get("PRETTY_NAME", ""), row.get("platform", ""),
                   f"base of {', '.join(row['used_by'])}" if row["used_by"] else ""]
        out("  " + "; ".join(part for part in details if part))


# -- services --

SERVICE_NAME = r"[a-z0-9][a-z0-9-]{0,23}"
SERVICE_LIMIT = 8
DEFAULTS = {"memory": "512m", "cpus": "1", "pids": 256, "ready_timeout": 60}
SPEC_KEYS = ("image", "port", "ready", "ready-timeout", "env", "secret", "data", "keep", "user", "read-only",
             "memory", "cpus", "pids")
ENV_NAME = r"[A-Za-z_][A-Za-z0-9_]{0,63}"
ENV_VALUE = r"[^\x00-\x1f\x7f,]{0,1000}"
SECRET_NAME = r"[A-Za-z_][A-Za-z0-9_]{0,63}"
MASKED_DATA = ("/", "/proc", "/sys", "/dev", "/etc", "/etc/resolv.conf", "/etc/hosts", "/run", "/tmp", "/usr", "/bin",
               "/sbin", "/lib", "/lib64")
SERVICE_LABEL = "sandbox-ai.service"
VOLUME_LABEL = "sandbox-ai.service-volume"


def default_service(name):
    return {"name": name, "image": name, "ports": [], "ready": None, "ready_timeout": DEFAULTS["ready_timeout"],
            "env": [], "secrets": [], "data": [], "keep": False, "user": None, "read_only": True,
            "memory": DEFAULTS["memory"], "cpus": DEFAULTS["cpus"], "pids": DEFAULTS["pids"]}


def yes_no(key, value):
    if value in ("yes", "true", "1", "on"):
        return True
    if value in ("no", "false", "0", "off"):
        return False
    raise ValueError(f"{key}= is yes or no: {value!r}")


def port_number(value, what):
    if not re.fullmatch(r"[0-9]{1,5}", value) or not 0 < int(value) < 65536:
        raise ValueError(f"{what} is a TCP port 1-65535: {value!r}")
    return int(value)


def parse_spec(value):
    """The service of --service NAME[:KEY=VALUE,...] (see the module's description), its syntax checked; what it
    names (the image, the secrets) is checked at the start (check)."""
    if not isinstance(value, str) or not value or len(value) > 4000:
        raise ValueError("--service expects NAME[:KEY=VALUE,...]")
    name, _, rest = value.partition(":")
    if not re.fullmatch(SERVICE_NAME, name):
        raise ValueError(f"--service {value!r}: a service name is lower-case letters, digits and '-' (up to 24)")
    service = default_service(name)
    for item in rest.split(",") if rest else []:
        key, sep, item_value = item.partition("=")
        key = key.strip()
        if not sep or key not in SPEC_KEYS:
            raise ValueError(f"--service {name}: {item!r}: expected KEY=VALUE with KEY one of {', '.join(SPEC_KEYS)}")
        if key == "image":
            if not valid_name(item_value):
                raise ValueError(f"--service {name}: image={item_value!r} is the name of one of your images "
                                 "(sandbox-ai images list)")
            service["image"] = item_value
        elif key == "port":
            port = port_number(item_value, f"--service {name}: port=")
            if port in service["ports"]:
                raise ValueError(f"--service {name}: port {port} is named twice")
            service["ports"].append(port)
        elif key == "ready":
            service["ready"] = "none" if item_value == "none" else port_number(item_value, f"--service {name}: ready=")
        elif key == "ready-timeout":
            if not re.fullmatch(r"[0-9]{1,3}", item_value) or not 1 <= int(item_value) <= 600:
                raise ValueError(f"--service {name}: ready-timeout= is 1 to 600 seconds")
            service["ready_timeout"] = int(item_value)
        elif key == "env":
            variable, has_value, text = item_value.partition("=")
            if not re.fullmatch(ENV_NAME, variable) or (has_value and not re.fullmatch(ENV_VALUE, text)):
                raise ValueError(f"--service {name}: env= is KEY=VALUE (or KEY: your variable KEY), KEY letters, digits "
                                 "and _, VALUE without commas or control characters")
            if any(entry["name"] == variable for entry in service["env"] + service["secrets"]):
                raise ValueError(f"--service {name}: {variable} is set twice")
            service["env"].append({"name": variable, **({"value": text} if has_value else {"from_host": True})})
        elif key == "secret":
            variable, _, secret = item_value.partition("=")
            secret = secret or variable
            if not re.fullmatch(ENV_NAME, variable) or not re.fullmatch(SECRET_NAME, secret):
                raise ValueError(f"--service {name}: secret= is KEY or KEY=SECRET (letters, digits, _)")
            if any(entry["name"] == variable for entry in service["env"] + service["secrets"]):
                raise ValueError(f"--service {name}: {variable} is set twice")
            service["secrets"].append({"name": variable, "secret": secret})
        elif key == "data":
            # One leading slash: normpath keeps two (POSIX), and //etc would be no system folder below.
            path = "/" + os.path.normpath(item_value).lstrip("/") if item_value.startswith("/") else ""
            if not path or not re.fullmatch(r"/[A-Za-z0-9._/@+-]{1,200}", path) or ".." in path.split("/") or \
                    path in MASKED_DATA or any(path.startswith(p + "/") for p in ("/proc", "/sys", "/dev")):
                raise ValueError(f"--service {name}: data= is an absolute path of a folder in the image (letters, "
                                 f"digits, . _ / @ + -), not a system folder: {item_value!r}")
            if path in service["data"]:
                raise ValueError(f"--service {name}: data={path} is named twice")
            service["data"].append(path)
        elif key == "keep":
            service["keep"] = yes_no("keep", item_value)
        elif key == "user":
            if not re.fullmatch(r"[0-9]{1,10}(?::[0-9]{1,10})?", item_value):
                raise ValueError(f"--service {name}: user= is UID or UID:GID (numbers: the firewall's checks hold "
                                 "for a number, never for a name)")
            uid, _, gid = item_value.partition(":")
            service["user"] = f"{int(uid)}:{int(gid) if gid else int(uid)}"
        elif key == "read-only":
            service["read_only"] = yes_no("read-only", item_value)
        elif key == "memory":
            if not re.fullmatch(r"[1-9][0-9]{0,6}[kmg]", item_value.lower()):
                raise ValueError(f"--service {name}: memory= is a size such as 512m or 2g")
            service["memory"] = item_value.lower()
        elif key == "cpus":
            if not re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,2})?", item_value) or not 0 < float(item_value) <= 512:
                raise ValueError(f"--service {name}: cpus= is a number such as 0.5 or 2")
            service["cpus"] = item_value
        elif key == "pids":
            if not re.fullmatch(r"[0-9]{2,6}", item_value) or int(item_value) < 16:
                raise ValueError(f"--service {name}: pids= is a number of at least 16")
            service["pids"] = int(item_value)
    if len(service["data"]) > 8 or len(service["ports"]) > 16 or len(service["env"]) + len(service["secrets"]) > 64:
        raise ValueError(f"--service {name}: at most 8 data folders, 16 ports and 64 variables")
    if isinstance(service["ready"], int) and service["ready"] not in service["ports"]:
        raise ValueError(f"--service {name}: ready={service['ready']} is none of its ports (port=)")
    if service["keep"] and not service["data"]:
        raise ValueError(f"--service {name}: keep=yes keeps its data folders: name them with data=PATH")
    return service


def spec_text(service):
    """The --service value of SERVICE (parse_spec's result, or the same keys): only what differs from the defaults,
    in a fixed order, so that parse_spec(spec_text(s)) == s."""
    items = []
    if service.get("image") and service["image"] != service["name"]:
        items.append(f"image={service['image']}")
    items += [f"port={port}" for port in service.get("ports", [])]
    ready = service.get("ready")
    if ready not in (None, "") and not (service.get("ports") and ready == service["ports"][0]):
        items.append(f"ready={ready}")
    if service.get("ready_timeout", DEFAULTS["ready_timeout"]) != DEFAULTS["ready_timeout"]:
        items.append(f"ready-timeout={service['ready_timeout']}")
    for entry in service.get("env", []):
        items.append(f"env={entry['name']}" + ("" if entry.get("from_host") else f"={entry.get('value', '')}"))
    for entry in service.get("secrets", []):
        items.append(f"secret={entry['name']}" + ("" if entry["secret"] == entry["name"] else f"={entry['secret']}"))
    items += [f"data={path}" for path in service.get("data", [])]
    if service.get("keep"):
        items.append("keep=yes")
    if service.get("user"):
        items.append(f"user={service['user']}")
    if service.get("read_only") is False:
        items.append("read-only=no")
    for key in ("memory", "cpus", "pids"):
        if str(service.get(key, DEFAULTS[key])) != str(DEFAULTS[key]):
            items.append(f"{key}={service[key]}")
    return service["name"] + (":" + ",".join(items) if items else "")


def ready_port(service):
    """The port the agent's start waits for, or None."""
    if service.get("ready") == "none":
        return None
    if isinstance(service.get("ready"), int):
        return service["ready"]
    return service["ports"][0] if service.get("ports") else None


def reserved_uids(l):
    """The users the sandbox's firewall lets past its checks (the connection proxy's, PulseAudio's, tor's): no
    service may run as one of them."""
    return {int(l.RUNTIME.PROXY_UID): "the connection proxy's", int(l.RUNTIME.AUDIO_UID): "the sound server's",
            int(l.load_vpn_module().runtime().TOR_UID): "tor's"}


def reserved_ports(l, a):
    rt = l.load_vpn_module().runtime()
    found = {int(l.PROXY_PORT): "the connection proxy", 53: "the sandbox's DNS", int(rt.TOR_TRANS_PORT): "Tor",
             int(rt.TOR_SOCKS_PORT): "Tor's SOCKS port"}
    for port in l.host_ports(a):
        found[port] = "a port of the host (--allow-host-port)"
    return found


def check(a, l=None, found=None):
    """The services of the start A (a.service: the --service values), checked; a.services gets them, each with its
    image's record ("record") and its user. ValueError for whatever the launcher refuses."""
    a.services = []
    values = list(getattr(a, "service", None) or [])
    if not values:
        return []
    l = l or launcher()
    if a.backend != "podman":
        raise ValueError("--service needs the Podman backend: a service is a container in the sandbox's Podman pod, "
                         "behind its firewall; Bubblewrap has no pod and no container engine to run it there")
    if a.podman_url:
        raise ValueError("--service with --podman-url: your images are recorded and pinned in this machine's Podman, "
                         "not in that one's; start it on that machine (sandbox-ai hosts run HOST -- ...)")
    if getattr(a, "mcp_socket_dir", None) or getattr(a, "helper_of", None):
        raise ValueError("--service is for an agent's start; an MCP server's or helper's sandbox has none")
    if l.tor_run(a):
        raise ValueError(f"--service is refused behind Tor (--vpn {a.vpn}): a service is an image nobody reviewed, "
                         "with Podman's system call filter instead of Firejail's beside the agent; software that "
                         "names itself in what it sends (an update check with an ID) makes a Tor run linkable; start "
                         "without one of them")
    if len(values) > SERVICE_LIMIT:
        raise ValueError(f"--service: at most {SERVICE_LIMIT} services")
    found = read_registry() if found is None else found
    uids, ports, services, seen = reserved_uids(l), reserved_ports(l, a), [], {}
    for value in values:
        service = parse_spec(value)
        if service["name"] in {s["name"] for s in services}:
            raise ValueError(f"--service {service['name']} is given twice")
        entry = record(service["image"], found)
        user = service["user"] or entry.get("user")
        if user is None:
            raise ValueError(f"--service {service['name']}: the user its image runs as ({entry.get('config_user')!r}) "
                             "is a name its /etc/passwd does not resolve; set it as a number: user=UID[:GID]")
        uid = int(user.partition(":")[0])
        if uid in uids:
            raise ValueError(f"--service {service['name']}: user {uid} is {uids[uid]}, which the sandbox's firewall "
                             "lets past its checks; choose another one (user=UID[:GID])")
        for port in service["ports"]:
            if port in ports:
                raise ValueError(f"--service {service['name']}: port {port} is {ports[port]}'s")
            if port in seen:
                raise ValueError(f"--service {service['name']}: port {port} is the service {seen[port]}'s too")
            seen[port] = service["name"]
        services.append({**service, "user": user, "record": entry})
    a.services = services
    return services


def notes(a):
    """What a start with services says about them before it runs."""
    found = []
    for service in getattr(a, "services", None) or []:
        entry = service["record"]
        where = ", ".join(f"localhost:{port}" for port in service["ports"]) or "no port named"
        found.append(f"service {service['name']}: your image {service['image']} ({entry['image']}), UNCHECKED (not "
                     f"reviewed by sandbox-ai); for the agent at {where}; behind the sandbox's firewall and proxy")
        if any(port < 1024 for port in service["ports"]):
            found.append(f"service {service['name']}: a port below 1024 needs a capability services never get; most "
                         "images can be set to a higher one")
        if service["read_only"] is False:
            found.append(f"service {service['name']}: its root file system is writable (read-only=no)")
    return found


def container_name(pod, service):
    return f"{pod}-service-{service['name']}"


def volume_key(a):
    """Whose kept data volumes a service uses: the kept start's (its key, stable across renames), else None (its data
    goes with the run)."""
    if not getattr(a, "keep_home", False):
        return None
    key = getattr(a, "start_key", None) or getattr(a, "start_id", None) or getattr(a, "session", None)
    return re.sub(r"[^A-Za-z0-9_.-]", "-", str(key))[:64] if key else None


def volume_name(key, service, index):
    """The kept volume of SERVICE's data folder INDEX in the start KEY. The key and the service's name both may hold
    '-' (start brisk-otter's service lantern-db, start brisk-otter-lantern's service db): an '_', which no service's
    name has, ends the key, so that no two starts' services ever share one volume."""
    return f"sandbox-ai-svc-{key}_{service['name']}-{index}"


def data_mounts(a, service):
    """[(path, kind, volume or None)]: kept volumes for keep=yes in a kept start, else tmpfs."""
    key = volume_key(a) if service.get("keep") else None
    return [(path, "volume", volume_name(key, service, index)) if key else (path, "tmpfs", None)
            for index, path in enumerate(service.get("data", []))]


def enforceable(a, l):
    """{memory, cpus, pids} a service's limits can be enforced for here (as the agent's: enforced_limits)."""
    keys = {"memory", "cpus", "pids"}
    if l.local_engine(a) and not l.user_manager_running():
        return set()
    controllers = l.delegated_controllers() if l.local_engine(a) else None
    if controllers is not None:
        keys = {key for key in keys if l.CGROUP_CONTROLLERS.get(key) in controllers}
    return keys


def create_command(a, pod, service, session, l=None, limits=None):
    """(podman create command, the environment it needs) of SERVICE in POD: created only (launch proves its
    namespaces before it starts it); secrets and your variables come from the environment (--env NAME), never the
    command line."""
    l = l or launcher()
    limits = enforceable(a, l) if limits is None else limits
    entry = service["record"]
    argv = l.podman_command(a, "create", "--pull=never", "--pod", pod, "--name", container_name(pod, service),
                            "--label", f"{l.RUN_LABEL}={a.run_id}", "--label", f"{SERVICE_LABEL}={service['name']}",
                            "--user", service["user"], "--cap-drop=ALL", "--security-opt=no-new-privileges",
                            "--init", "--log-driver=k8s-file", "--log-opt", "max-size=10mb",
                            # The image's own VOLUMEs: a tmpfs each, gone with the run (never a volume left behind).
                            "--image-volume=tmpfs")
    if service.get("read_only", True):
        argv.append("--read-only")
    if "memory" in limits:
        argv += ["--memory", service["memory"]]
    if "cpus" in limits:
        argv += ["--cpus", str(service["cpus"])]
    if "pids" in limits:
        argv += ["--pids-limit", str(service["pids"])]
    # The sandbox's own resolver and pinned names: its lookups meet the same DNS (stub, allow-list) as the agent's.
    for name in ("resolv.conf", "hosts"):
        argv += l.mount_option(Path(session) / name, f"/etc/{name}", "ro")
    for path, kind, volume in data_mounts(a, service):
        if kind == "volume":
            argv += ["--volume", f"{volume}:{path}:rw,nosuid,nodev,U"]
        else:
            argv += ["--tmpfs", f"{path}:rw,noexec,nosuid,nodev,size={service['memory']}"]
    env = {}
    for item in service.get("env", []):
        if item.get("from_host"):
            argv += ["--env", item["name"]]
            env[item["name"]] = os.environ.get(item["name"], "")
        else:
            argv += ["--env", f"{item['name']}={item.get('value', '')}"]
    for item in service.get("secrets", []):
        argv += ["--env", item["name"]]
        env[item["name"]] = item.get("value", "")
    return argv + [entry["id"].removeprefix("sha256:")], env


def plan(a, l=None):
    """The dry run's account of the services."""
    l = l or launcher()
    found, enforced = [], sorted(enforceable(a, l))
    for service in getattr(a, "services", None) or []:
        entry = service["record"]
        found.append({"name": service["name"], "image": service["image"], "reference": entry["image"],
                      "kind": entry["kind"], "id": entry["id"], "digest": entry.get("digest"),
                      "unchecked": not entry.get("reviewed"), "user": service["user"],
                      "reachable_at": [f"localhost:{port}" for port in service["ports"]],
                      "ready": ready_port(service), "ready_timeout": service["ready_timeout"],
                      "read_only": service["read_only"],
                      "limits": {"memory": service["memory"], "cpus": service["cpus"], "pids": service["pids"]},
                      "limits_enforced": enforced,
                      "data": [{"path": path, "kind": kind, **({"volume": volume} if volume else {})}
                               for path, kind, volume in data_mounts(a, service)],
                      "env": [item["name"] for item in service["env"]],
                      "secrets": [item["secret"] for item in service["secrets"]],
                      "capabilities": "none (--cap-drop=ALL, no-new-privileges)",
                      "network": "the sandbox's own: its firewall, connection proxy, allow-list, approvals, kill "
                                 "switch and VPN, as the agent's",
                      "started": "after the sandbox's firewall and proxy are up, before the agent"})
    return found


def add_again(name, entry):
    """The command that adds your image NAME (its record ENTRY) again, pinned anew."""
    if entry["kind"] == "pulled":
        return f"sandbox-ai images pull {entry.get('ref') or entry['image']} --as {name} --replace"
    return f"sandbox-ai images build-image {name} --file {entry.get('containerfile')} --replace"


def prepare(a, l=None, run=None):
    """At a real start (not a dry run): every service's image present with its recorded ID, its secrets and your
    variables there. Fills in the secrets' values (never printed)."""
    l = l or launcher()
    run = run or subprocess.run
    store = None
    for service in getattr(a, "services", None) or []:
        entry = service["record"]
        image_id = entry["id"].removeprefix("sha256:")
        try:
            done = run(l.podman_command(a, "image", "exists", image_id), stdin=subprocess.DEVNULL, capture_output=True,
                       timeout=60)
        except FileNotFoundError:
            raise ValueError("Required tool missing: podman") from None
        if done.returncode != 0:
            how = add_again(service["image"], entry)
            raise ValueError(f"--service {service['name']}: the image {service['image']} ({entry['image']}, "
                             f"{image_id[:12]}) is not in Podman's store (removed, or replaced since it was added); "
                             f"nothing is pulled at a start: {how}")
        for item in service["env"]:
            if item.get("from_host") and item["name"] not in os.environ:
                raise ValueError(f"--service {service['name']}: env={item['name']}: your environment has no "
                                 f"{item['name']}")
        for item in service["secrets"]:
            store = store or l.load_secret_store_module()
            value = store.get(item["secret"])
            if value is None:
                raise ValueError(f"--service {service['name']}: no secret {item['secret']}; keep one with: sandbox-ai "
                                 f"secrets set {item['secret']}")
            item["value"] = value


def supervisor_config(a):
    """What the sandbox's supervisor waits for before the agent starts (runtime.await_services)."""
    return [{"name": service["name"], "ready": ready_port(service), "timeout": service["ready_timeout"]}
            for service in getattr(a, "services", None) or []]


def record_entries(a, pod):
    """The services in run.json: sandbox-ai list shows them."""
    return [{"name": service["name"], "image": service["image"], "reference": service["record"]["image"],
             "ports": service["ports"], "container": container_name(pod, service), "unchecked": True}
            for service in getattr(a, "services", None) or []]


def write_answer(directory, answer, l):
    path = Path(directory) / l.RUNTIME.SERVICES_STARTED
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(answer))
    os.replace(temporary, path)


def tailnet_only(a, vpn):
    """Whether the start's VPN profile is Tailscale without an exit node: it carries only the tailnet and loads no kill
    switch (vpn_runtime.up_tailscale), so the agent's other connections - and a service's, in its namespace - leave as
    without a VPN. False whenever that is not known (a profile that cannot be read): the kill switch is checked then."""
    profile = getattr(a, "vpn_profile", None)
    if not isinstance(profile, dict):
        try:
            profile = vpn.load_profile(a.vpn)
        except (OSError, ValueError):
            return False
    return isinstance(profile, dict) and profile.get("type") == "tailscale" and \
        not (profile.get("tailscale") or {}).get("exit_node")


def check_vpn(a, pod, service, l, run=None, started=True):
    """With a VPN (--vpn: vpn.agent_up made POD with the tunnel's container in it), SERVICE's container is proven to
    be in the VPN container's network namespace, with the kill switch in place (vpn.check_pod_vpn, as an MCP server's
    pod is; a Tailscale profile without an exit node has none: tailnet_only); ValueError otherwise - the run does not
    start (launch answers the supervisor so, and removes the container). STARTED false: before its start (created
    and initialised, not running yet). Nothing without one."""
    if not getattr(a, "vpn", None):
        return
    vpn = l.load_vpn_module()
    checked = (lambda command, **kwargs: run(command, stdin=subprocess.DEVNULL, **kwargs)) if run else None
    try:
        vpn.check_pod_vpn(l.podman_command(a), pod, a.vpn, checked, member=container_name(pod, service),
                          killswitch=not tailnet_only(a, vpn), member_running=started)
    except ValueError as exc:
        raise ValueError(f"it is not behind the VPN {a.vpn} as it must be ({exc})") from None


def remove_container(a, pod, service, l, run):
    """SERVICE's container goes at once (the pod goes with the run anyway)."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        run(l.podman_command(a, "rm", "--force", "--time", "0", container_name(pod, service)), stdin=subprocess.DEVNULL,
            capture_output=True, timeout=120)


def check_userns(a, pod, service, l, run, readlink=os.readlink):
    """SERVICE's container is in the user namespace of POD's infra container, which owns the pod's network namespace:
    the firewall knows the proxy's (and tor's) packets by their uid as that namespace sees it (meta skuid), and a
    container in another one (containers.conf's userns = keep-id or auto) would be other uids there - its root the
    proxy's uid 1, past the redirect to the proxy. Podman takes no --userns with --pod, so it is proven once the
    container is initialised (podman init: its namespaces made, nothing of the image run yet), before its start: on
    this host's engine by the /proc/PID/ns/user of the infra container's process and of the service's (waiting to
    run), on podman machine's (macOS: its processes are in the VM) by the UsernsMode Podman gives the service and the
    agent's container, made alike in the pod. ValueError
    otherwise (and when it cannot be told)."""
    member = container_name(pod, service)

    def answer(*words):
        done = run(l.podman_command(a, *words), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        lines = (done.stdout or "").splitlines()  # one line each, an empty one too (UsernsMode "": the default)
        if done.returncode != 0:
            raise ValueError(f"podman cannot tell ({(done.stderr or '').strip()[-200:] or 'no answer'})")
        return [line.strip() for line in lines]
    if l.local_engine(a):
        infra = answer("pod", "inspect", "--format", "{{.InfraContainerID}}", pod)
        if len(infra) != 1 or not re.fullmatch(r"[0-9a-f]{64}", infra[0]):
            raise ValueError(f"the pod {pod} names no infra container")
        pids = answer("inspect", "--type", "container", "--format", "{{.State.Pid}}", infra[0], member)
        if len(pids) != 2 or not all(pid.isdigit() and int(pid) > 0 for pid in pids):
            raise ValueError(f"the processes of {member} and the pod's infra container are not there")
        own = [readlink(f"/proc/{pid}/ns/user") for pid in pids]
        if own[0] != own[1]:
            raise ValueError(f"its user namespace is not the pod's ({own[1]}, not {own[0]}): the firewall would not "
                             "know its users (a userns setting of containers.conf?)")
        return
    modes = answer("inspect", "--type", "container", "--format", "{{.HostConfig.UsernsMode}}", f"{pod}-agent", member)
    if len(modes) != 2:
        raise ValueError(f"podman's answer about the user namespaces of {member} is not understood")
    if modes[0] != modes[1]:
        raise ValueError(f"its user namespace is not the agent's ({modes[1] or 'the default'}, not "
                         f"{modes[0] or 'the default'}): the firewall would not know its users (a userns setting of "
                         "containers.conf?)")


def launch(a, pod, session, alive, l=None, run=None, sleep=time.sleep, out=None, readlink=None):
    """The launcher's half of the services' start (the supervisor's: runtime.await_services). Waits for the
    supervisor's SERVICES_GO in the run's directory (its firewall and proxy are up), then starts each service in POD
    and answers in SERVICES_STARTED: {"ok": true} or {"ok": false, "error": ...}. ALIVE(): the run still starts.
    Returns the answer, or None when the run ended first."""
    l = l or launcher()
    run = run or subprocess.run
    out = out or (lambda text: print(f"agent-sandbox: {text}", file=sys.stderr))
    directory = Path(a.control_dir)
    # As long as the run starts: its supervisor writes SERVICES_GO before its agent runs, or ends.
    while not (directory / l.RUNTIME.SERVICES_GO).exists():
        if not alive():
            return None
        sleep(0.1)
    started = []
    try:
        limits = enforceable(a, l)  # (in here: whatever fails, the supervisor gets its answer)
        for service in a.services:
            for path, kind, volume in data_mounts(a, service):
                if kind == "volume" and run(l.podman_command(a, "volume", "exists", volume), stdin=subprocess.DEVNULL,
                                            capture_output=True, timeout=60).returncode != 0:
                    made = run(l.podman_command(a, "volume", "create", "--label", f"{VOLUME_LABEL}={volume_key(a)}",
                                                volume), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=60)
                    if made.returncode != 0:
                        raise ValueError(f"the volume {volume} for {path}: {(made.stderr or '').strip()[-300:]}")
            # Created, initialised (its namespaces made, nothing of the image run: podman init), proven, and only then
            # started: nothing of it ever runs outside the pod's user namespace or the VPN's network namespace.
            argv, env = create_command(a, pod, service, session, l, limits)
            done = run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300,
                       env={**os.environ, **env})
            if done.returncode != 0:
                remove_container(a, pod, service, l, run)  # (a half-made one)
                raise ValueError(f"{(done.stderr or '').strip()[-400:] or 'podman create failed'}")
            try:
                step = "it could not be initialised"
                done = run(l.podman_command(a, "init", container_name(pod, service)), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=120)
                if done.returncode != 0:
                    raise ValueError((done.stderr or "").strip()[-300:] or "podman init failed")
                step = "it is not in the pod's user namespace"
                check_userns(a, pod, service, l, run, readlink or os.readlink)
                step = "before its start"
                check_vpn(a, pod, service, l, run, started=False)
                step = "it could not be started"
                done = run(l.podman_command(a, "start", container_name(pod, service)), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=300)
                if done.returncode != 0:
                    raise ValueError((done.stderr or "").strip()[-300:] or "podman start failed")
                step = "after its start"
                check_vpn(a, pod, service, l, run)  # its VPN container still the one checked, with its kill switch
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                remove_container(a, pod, service, l, run)
                raise ValueError(f"{step} ({exc}); it was removed" if step.startswith("it ") else
                                 f"{exc} ({step}); it was removed") from None
            started.append(service["name"])
            out(f"service {service['name']} started ({container_name(pod, service)}; logs: podman logs "
                f"{container_name(pod, service)})")
        answer = {"ok": True, "services": started}
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the supervisor gets an answer (never waits on)
        name = next((s["name"] for s in a.services if s["name"] not in started), "?")
        answer = {"ok": False, "error": l.printable(f"the service {name} did not start: {exc}")[:600]}
    try:
        write_answer(directory, answer, l)
    except OSError as exc:
        out(f"the services' answer could not be written ({exc}); the sandbox does not start")
    return answer


def collect_logs(a, pod, l=None, run=None):
    """Before the pod goes: each service's output into the log directory (services-NAME.log), when there is one."""
    l = l or launcher()
    run = run or subprocess.run
    if not getattr(a, "services", None) or not a.log_dir:
        return []
    folder = Path(a.log_dir).expanduser()
    written = []
    for service in a.services:
        try:
            done = run(l.podman_command(a, "logs", "--tail", "5000", container_name(pod, service)),
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode != 0:
            continue
        data = (done.stdout or b"") + (done.stderr or b"")
        data = data if isinstance(data, bytes) else str(data).encode()
        target = folder / f"service-{service['name']}-{a.run_id}.log"
        with contextlib.suppress(OSError):
            images_module().write_private(target, data[-(4 << 20):])
            written.append(target)
    return written


def forget_volumes(key, podman=None, run=None):
    """Remove the kept data volumes of the start KEY (sandbox-ai delete). The names of those removed."""
    podman = podman or images_module().podman()
    key = re.sub(r"[^A-Za-z0-9_.-]", "-", str(key))[:64]
    try:
        listed = run_podman([*podman, "volume", "ls", "--quiet", "--filter", f"label={VOLUME_LABEL}={key}"], run,
                            timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    names = [line.strip() for line in (listed.stdout or "").splitlines() if line.strip()] \
        if listed.returncode == 0 else []
    removed = []
    for name in names:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            if run_podman([*podman, "volume", "rm", name], run, timeout=60).returncode == 0:
                removed.append(name)
    return removed


# -- the command (sandbox-ai images pull|build-image|list; remove: images.py) --

def main(argv, out=print, run=None):
    parser = argparse.ArgumentParser(prog="sandbox-ai images", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pulling = sub.add_parser("pull")
    pulling.add_argument("reference")
    pulling.add_argument("--as", dest="name")
    pulling.add_argument("--replace", action="store_true")
    pulling.add_argument("-j", "--json", action="store_true")
    building = sub.add_parser("build-image")
    building.add_argument("name")
    building.add_argument("-f", "--file", required=True)
    building.add_argument("--context")
    building.add_argument("--replace", action="store_true")
    building.add_argument("--trust-agent-written", action="store_true",
                          help="build a Containerfile or context that lies where a sandbox can write (you reviewed it)")
    building.add_argument("--build-network", action="store_true",
                          help="give its RUN steps the network: this host's own, outside the sandbox's firewall, VPN "
                               "and Tor (default: none)")
    building.add_argument("-j", "--json", action="store_true")
    listed = sub.add_parser("list")
    listed.add_argument("-j", "--json", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "pull":
        name, entry = pull(args.reference, args.name, args.replace, run=run,
                           out=(lambda text: print(text, file=sys.stderr)) if args.json else out)
        if args.json:
            out(json.dumps({"name": name, **entry}, indent=2))
        return 0
    if args.command == "build-image":
        name, entry = build(args.name, args.file, args.context, args.replace, run=run,
                            out=(lambda text: print(text, file=sys.stderr)) if args.json else out,
                            trust_agent_written=args.trust_agent_written, network=args.build_network)
        if args.json:
            out(json.dumps({"name": name, **entry}, indent=2))
        return 0
    rows = listing(run=run)
    if args.json:
        out(json.dumps(rows, indent=2))
    else:
        show_rows(rows, out)
    return 0
