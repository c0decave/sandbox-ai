#!/usr/bin/env python3
"""sandbox-ai images: the agent images on this host, what is in them, and your extensions of them.

  sandbox-ai images [--json] [--podman-url URL]          images and variants: agent version, created, size,
                                                         whether each is current with its extension
  sandbox-ai images packages AGENT|VARIANT [--json]      Debian packages, global npm packages and tools in one
  sandbox-ai images extend AGENT|VARIANT [--add-os PKG] [--remove-os PKG] [--add-npm PKG] [--remove-npm PKG]
                     [--add-pip PKG] [--remove-pip PKG] [--add-file SRC:TARGET[:MODE]] [--remove-file TARGET]
                     [--fragment FILE|-] [--clear-fragment] [--show] [--json]
  sandbox-ai images check AGENT|VARIANT [--fragment FILE|-] [--json]
                                                         validate the extension, show the final Containerfile
  sandbox-ai images recipe NAME --from AGENT [--as V]    a variant with a ready extension (media: ffmpeg,
                                                         PulseAudio, espeak-ng, Piper ... for --audio virtual and
                                                         videos; dev: Podman and tools for developing sandbox-ai,
                                                         with --nested-containers; hyperframes, playwright: the
                                                         CLIs of the built-in kits' skills, pinned by lockfiles;
                                                         android: adb, apktool, smali, jadx and Frida for the
                                                         preset android-debugging)
  sandbox-ai images new NAME --from AGENT [--empty]      a variant: localhost/AGENT-sandbox-NAME, started with
                                                         --image-variant NAME; each agent has its own (the recipe
                                                         media for claude and for dsh); where a name is a variant
                                                         of several agents, commands take it as NAME@AGENT
  sandbox-ai images remove AGENT|VARIANT [--keep-image]  drop an extension (and a variant's image)
  sandbox-ai images history [AGENT|VARIANT] [--json]     changes and builds of the extensions
  sandbox-ai images new NAME --from AGENT --base IMAGE   a variant built on one of your images (Debian 12) instead
                                                         of the agent's own base: the agent's layers on top of it
  sandbox-ai images pull REFERENCE [--as NAME] [--replace]
                                                         one of your images from a registry (Docker Hub, quay.io,
                                                         ghcr.io, ...): pulled once, now, and pinned by its digest
  sandbox-ai images build-image NAME --file CONTAINERFILE [--context DIR] [--replace] [--trust-agent-written]
                             [--build-network]
                                                         one of your images from your own Containerfile: each FROM
                                                         pulled once and pinned by digest, the result by its ID;
                                                         its RUN steps without network unless --build-network (this
                                                         host's own, outside the firewall, VPN and Tor); refused
                                                         where a sandbox can write unless --trust-agent-written
  sandbox-ai images build-image NAME --builtin TEMPLATE  the same of one of sandbox-ai's own Containerfiles
                                                         (sandbox/service-images; showcase-web: nginx for the
                                                         showcase presets), unchecked like yours
  sandbox-ai images list [--json]                        your images, each marked UNCHECKED (nobody reviewed them)
  sandbox-ai images templates [--json]                   sandbox-ai's own Containerfiles for services (--builtin),
                                                         each with the images of yours built from it
  sandbox-ai images os AGENT|VARIANT|IMAGE [--json]      its distribution and the package manager of its OS packages
  sandbox-ai images search AGENT|VARIANT|IMAGE [QUERY] [--refresh] [--limit N] [--json]
                                                         search the OS packages of its distribution: the package
                                                         index (apt, apk, dnf, pacman, zypper) read once in a
                                                         throwaway container of the image, with the network an
                                                         extension's build has, cached per image
  sandbox-ai images derive NAME --from IMAGE --add-os PKG [--add-os PKG ...] [--replace]
                                                         your IMAGE with OS packages added by its own distribution's
                                                         manager (Alpine: apk, Fedora/RHEL: dnf, Arch: pacman,
                                                         openSUSE: zypper, Debian/Ubuntu: apt-get), one of your
                                                         images again (UNCHECKED)
  sandbox-ai images remove IMAGE [--keep-image]          forget one of your images

Your images run as services beside the agent (sandbox-ai claude --service NAME[:port=PORT,...]: behind the
sandbox's firewall and proxy, no capabilities, a read-only root, limits of their own) or as the base of a
variant. Starts never pull: an image is pinned when you add it, and its age does not matter.

An extension adds OS packages (the agent images are Debian 12: apt-get; --add-os, also --add-apt), global npm
packages, Python tools (uv tool), files and an optional Containerfile fragment on top of the agent's base image. Your
own images may be another distribution (Alpine, Fedora/RHEL, Arch, openSUSE): `images derive` adds OS packages to
them with that distribution's manager. Extensions live in your configuration directory
(~/.config/sandbox-ai/images/NAME), never in a workspace or anything a sandbox can write; sandbox-ai build
builds the base image and then the extension layer, labelled with a hash of the extension. Standard library
only.
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import threading

LOADING = threading.RLock()  # the lazily loaded helper modules below

HERE = Path(__file__).resolve().parent


def launch_log():
    """launchlog.py, the one copy of this process (launcher.log; DEBUG lines with SANDBOX_AI_DEBUG=1)."""
    loaded = sys.modules.get("sandbox_ai_launchlog")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_launchlog", HERE / "launchlog.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded = sys.modules.setdefault("sandbox_ai_launchlog", module)
    return loaded


LOG = launch_log()
AGENTS = ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi")
LABEL = "sandbox-ai.agent-version"
HASH_LABEL = "sandbox-ai.extension-hash"
BASE_LABEL = "sandbox-ai.base-id"
EXTENSION_LABEL = "sandbox-ai.extension"
AGENT_PACKAGES = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex", "opencode": "opencode-ai"}
NAME = r"[a-z0-9][a-z0-9_.-]{0,29}"
RESERVED = {*AGENTS, "all", "base", "latest", "sandbox", "team", "vpn", "variants"}  # team, vpn: build targets
VARIANTS = "variants"  # config_root()/variants/AGENT/NAME: each agent's variants (a recipe's name for every agent)
KINDS = ("apt", "npm", "pip")
DOCUMENT = "image.json"
FRAGMENT = "Containerfile.fragment"
FILES = "files"
FRAGMENT_LIMIT = 64 << 10
FILE_LIMIT = 50 << 20
FILES_LIMIT = 200 << 20
PACKAGE_LIMIT = 200
FILE_COUNT_LIMIT = 100
PACKAGE_PATTERNS = {
    "apt": r"[a-z0-9][a-z0-9+.-]{0,99}(=[A-Za-z0-9.+~:-]{1,64})?",
    "npm": r"(@[a-z0-9][a-z0-9._~-]{0,63}/)?[a-z0-9][a-z0-9._~-]{0,127}(@[A-Za-z0-9.^~*+-]{1,64})?",
    "pip": r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?(\[[A-Za-z0-9._-]+(?:,[A-Za-z0-9._-]+)*\])?"
           r"((?:==|>=|<=|~=|!=|<|>)[A-Za-z0-9.*+!-]{1,64})?",
}
PACKAGE_HELP = {"apt": "a Debian package name, optionally =VERSION",
                "npm": "an npm package name (@scope/name), optionally @VERSION; no URLs, paths or git sources",
                "pip": "a Python package name, optionally [extras] and ==VERSION (or >=, ~=, ...); no URLs or paths"}

# What the sandbox's own setup runs as root inside the container (runtime.py) or relies on: an extension
# that replaces, shadows or removes one of these could switch off the firewall or the privilege drop.
PROTECTED_TOOLS = ("python3", "nft", "firejail", "setpriv", "tmux", "Xvfb", "x11vnc", "sh", "bash")
PROTECTED_RE = re.compile(
    r"(?<![\w.-])(?:(?:/usr)?(?:/local)?/s?bin/(?:python3[0-9.]*|nft|firejail|setpriv|tmux|Xvfb|x11vnc|sh|bash)"
    r"(?![\w.-])|/usr/lib/python3[0-9.]*(?![\w.-])|/etc/firejail(?![\w.-])|/etc/ld\.so\.preload|/etc/nftables"
    r"|/sandbox(?![\w.-])|/etc/ssl/certs/ca-certificates\.crt)")
PROTECTED_PACKAGES = ("firejail", "nftables", "python3", "python3-minimal", "util-linux", "tmux", "bash",
                      "ca-certificates", "xvfb", "x11vnc", "libc6")
# Mounted over when a sandbox starts: what an image puts there is never seen.
MASKED = ("/work", "/workspaces", "/root", "/tmp", "/var/tmp", "/run", "/proc", "/sys", "/dev")

# The validator's rules: id -> (severity, explanation). "error" refuses saving and building.
RULES = {
    "from": ("error", "FROM is not allowed: an extension always builds on the agent's own base image"),
    "entrypoint": ("error", "ENTRYPOINT and CMD are not allowed: the launcher starts its own setup, which the "
                            "image must not replace"),
    "volume": ("error", "VOLUME is not allowed: it would give the sandbox a writable, persistent directory on "
                        "the host past the read-only root file system"),
    "onbuild": ("error", "ONBUILD is not allowed"),
    "unknown": ("error", "unknown instruction"),
    "heredoc": ("error", "here-documents (<<EOF) are not allowed: write the commands as RUN lines, so that the "
                         "validator sees them"),
    "syntax": ("error", "cannot be parsed"),
    "setuid": ("error", "setuid or setgid bits are not allowed: a program that gains rights when the agent "
                        "starts it breaks the sandbox's no-new-privileges assumption"),
    "setcap": ("error", "file capabilities (setcap) are not allowed"),
    "protected": ("error", "changes a program or file the sandbox's setup runs or trusts as root (python3, nft, "
                           "firejail, setpriv, tmux, sh, bash, the loader, /sandbox, the CA bundle)"),
    "remove-protected": ("error", "removes a package the sandbox's setup needs (firejail, nftables, python3, "
                                  "util-linux, tmux, bash, ca-certificates, ...)"),
    "add-url": ("error", "ADD from a URL needs --checksum=sha256:...: otherwise whatever the server sends at "
                         "build time ends up in the image"),
    "add-git": ("error", "ADD from a Git repository is not allowed; clone it in a RUN line at a fixed commit"),
    "copy-from": ("error", "COPY --from takes files from another image or stage, which is not allowed"),
    "copy-source": ("error", "a COPY source must be a file of this extension: files/NAME (add it with "
                             "sandbox-ai images extend TARGET --add-file SRC:TARGET)"),
    "loader": ("error", "LD_PRELOAD, LD_AUDIT and LD_LIBRARY_PATH would be loaded into every program, the "
                        "sandbox's setup included"),
    "label": ("error", "labels sandbox-ai.* are set by sandbox-ai itself"),
    "size": ("error", f"the fragment is larger than {FRAGMENT_LIMIT >> 10} KiB"),
    "control": ("error", "control characters are not allowed"),
    "package": ("error", "not a valid package"),
    "agent-package": ("error", "the agent itself comes with the base image (pin it with sandbox-ai build --pin)"),
    "file": ("error", "not a usable file"),
    "mode": ("error", "files writable by every user are not allowed"),
    "add-local": ("warning", "ADD unpacks archives; use COPY for a plain file"),
    "pipe-shell": ("warning", "runs a script straight from the network: nothing checks what is downloaded"),
    "user": ("warning", "the launcher starts every sandbox as root of its user namespace (--user=0:0) and drops "
                        "privileges itself; the extension ends with USER root again"),
    "workdir": ("warning", "the launcher always starts in /work; the extension ends with WORKDIR /work again"),
    "shell": ("warning", "SHELL changes how every later RUN line is run"),
    "env": ("warning", "the launcher or its connection proxy sets this variable at start; the image's value is "
                       "replaced or may break the proxy"),
    "ca-trust": ("warning", "adds trusted certificate authorities to the image"),
    "masked": ("warning", "this directory is mounted over when a sandbox starts: the file will not be visible"),
    "no-effect": ("warning", "has no effect in a sandbox"),
    "unknown-flag": ("warning", "unknown option"),
}
ALLOWED = {"RUN", "ENV", "COPY", "ADD", "ARG", "LABEL", "WORKDIR", "USER", "SHELL", "EXPOSE", "HEALTHCHECK",
           "STOPSIGNAL", "MAINTAINER"}
WATCHED_ENV = {"PATH", "HOME", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
               "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "NODE_OPTIONS", "BASH_ENV",
               "ENV", "GIT_SSL_CAINFO", "PIP_CERT", "UV_NATIVE_TLS", "NODE_USE_ENV_PROXY"}
LOADER_ENV = {"LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH"}


# -- where extensions live --

def config_root():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "images"


def history_file():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "images" / \
        "history.jsonl"


_launcher = None


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_images", HERE / "agent_sandbox.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _launcher is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _launcher = loaded
    return _launcher


def check_location(path, what):
    """Refuse PATH (as given) when a sandbox could have written it: inside a tree a sandbox could write (the
    launcher's registry of exposed trees) or reached through a symlink planted in one."""
    l = launcher()
    before = l.exposed_before()
    l.check_planted(str(path), what, before)
    resolved = Path(os.path.abspath(os.path.expanduser(str(path)))).resolve()
    for tree in before:
        if resolved == tree or tree in resolved.parents:
            raise ValueError(f"{what} {resolved} lies inside {tree}, which a sandbox can write; keep image "
                             "extensions and the files they copy outside workspaces, rw mounts and sandbox HOMEs")


def valid_name(name):
    return isinstance(name, str) and bool(re.fullmatch(NAME, name)) and name not in RESERVED


# A part of an image name as Podman takes it (the OCI reference grammar): letters and digits, separated by one "." or
# "_", by "__" or by dashes - never at the end, never two dots. A name Podman cannot tag is refused when it is made.
TAG_SAFE = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*")


def taggable(name):
    """NAME is valid_name and Podman can tag an image with it (localhost/AGENT-sandbox-NAME, sandbox-ai-image-NAME)."""
    return valid_name(name) and bool(TAG_SAFE.fullmatch(name))


NAME_RULE = ("lower-case letters and digits, joined by '.', '_' or '-' (not at the end, no '..' or '___': part of an "
             "image name), up to 30 characters")


def bare(target):
    """The variant's own name of TARGET (NAME, or NAME@AGENT: the variant NAME of AGENT)."""
    return str(target).partition("@")[0]


def image_name(target, agent):
    return f"localhost/{agent}-sandbox:latest" if target == agent else \
        f"localhost/{agent}-sandbox-{bare(target)}:latest"


def base_image(agent):
    return f"localhost/{agent}-sandbox:base"


def base_of(target, document):
    """The image TARGET's extension layer is built on: the agent's base, or for a variant on a base image of yours
    (sandbox-ai images new NAME --base IMAGE) its own: the agent's layers built on that image."""
    agent = document["agent"]
    return f"localhost/{agent}-sandbox-{bare(target)}:base" if document.get("base") and target != agent else \
        base_image(agent)


def prep_image(target, agent):
    """Your base image made ready for the agent's layers (prep_containerfile), tagged for sandbox-ai build."""
    return f"localhost/{agent}-sandbox-{bare(target)}:prep"


def read_limited(path, limit):
    """Bytes of the regular file PATH (never through a final symlink), at most LIMIT, else ValueError."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise ValueError(f"{path}: {exc.strerror}") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{path}: not a regular file")
        if info.st_size > limit:
            raise ValueError(f"{path}: larger than {limit >> 10} KiB")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path}: larger than {limit >> 10} KiB")
    return data


def empty_document(agent):
    return {"version": 1, "agent": agent, "apt": [], "npm": [], "pip": [], "files": []}


def document_agent(directory):
    """The agent the extension in DIRECTORY is of (its image.json), else None."""
    try:
        document = json.loads(read_limited(directory / DOCUMENT, 1 << 20))
    except (ValueError, OSError, RecursionError):
        return None
    return document.get("agent") if isinstance(document, dict) and document.get("agent") in AGENTS else None


def migrate_variants(root):
    """The variants of before 0.1.95's fixes (config_root()/NAME, one agent per name) moved to their agent's folder
    (config_root()/variants/AGENT/NAME): a recipe's variant (tor, media) can then be made for every agent that needs
    it. Once per variant; one that cannot be moved stays where it is and is read there."""
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return
    for name in entries:
        if not valid_name(name) or (root / name).is_symlink():
            continue
        agent = document_agent(root / name)
        if agent is None:
            continue
        target = root / VARIANTS / agent / name
        if target.exists() or target.is_symlink():
            continue
        try:
            check_location(root, "The image extensions in")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.rename(root / name, target)
        except (OSError, ValueError):
            continue


def variants():
    """[(name, agent)] of the variants in the configuration directory (each agent's own: the same name may be a
    variant of several agents), sorted by name, then agent."""
    root = config_root()
    if not root.is_dir():
        return []
    migrate_variants(root)
    found = set()
    for agent in AGENTS:
        folder = root / VARIANTS / agent
        try:
            names = os.listdir(folder) if not folder.is_symlink() else []
        except OSError:
            names = []
        for name in names:
            if valid_name(name) and document_agent(folder / name) == agent:
                found.add((name, agent))
    for name in os.listdir(root):  # (one that could not be moved: read where it is)
        if valid_name(name) and not (root / name).is_symlink():
            agent = document_agent(root / name)
            if agent is not None:
                found.add((name, agent))
    return sorted(found)


def targets():
    """[(target, agent)]: the agents' own extensions first, then the variants - each by its name, or NAME@AGENT where
    the same name is a variant of several agents (shown_target)."""
    found = variants()
    return [(agent, agent) for agent in AGENTS] + [(shown_target(name, agent, found), agent) for name, agent in found]


def shown_target(name, agent, found=None):
    """The variant NAME of AGENT as its commands take it: NAME, or NAME@AGENT when NAME is a variant of another agent
    too."""
    found = variants() if found is None else found
    return f"{name}@{agent}" if [owner for other, owner in found if other == name and owner != agent] else name


def resolve(target):
    """(name, agent) of TARGET: an agent (its own extension), a variant's NAME (of one agent only), or NAME@AGENT;
    else ValueError."""
    if target in AGENTS:
        return target, target
    name, at, agent = str(target).partition("@")
    if not valid_name(name) or (at and agent not in AGENTS):
        raise ValueError(f"{target!r} is neither an agent ({', '.join(AGENTS)}) nor a variant name "
                         "(lower-case letters, digits, '.', '_' and '-', up to 30 characters; NAME@AGENT for the "
                         "variant NAME of AGENT)")
    owners = [owner for other, owner in variants() if other == name]
    if at:
        if agent in owners:
            return name, agent
        raise ValueError(f"no variant {name} of {agent}; create it with: sandbox-ai images new {name} --from {agent}")
    if len(owners) == 1:
        return name, owners[0]
    if owners:
        raise ValueError(f"{name} is a variant of {' and '.join(owners)}: name one of them, e.g. {name}@{owners[0]}")
    raise ValueError(f"no variant {target}; create it with: sandbox-ai images new {target} --from AGENT")


def agent_of(target):
    """The agent of TARGET (an agent or an existing variant: NAME, NAME@AGENT), else ValueError."""
    return resolve(target)[1]


def target_dir(target):
    """The folder of TARGET's extension: the agent's own (config_root()/AGENT), a variant's in its agent's folder (or
    where one of before was left: migrate_variants)."""
    name, agent = resolve(target)
    return config_root() / agent if name == agent else variant_dir(name, agent)


def variant_dir(name, agent):
    """The folder of the variant NAME of AGENT: in its agent's folder, or where one of before was left."""
    root = config_root()
    own = root / VARIANTS / agent / name
    if not own.exists() and not (root / name).is_symlink() and document_agent(root / name) == agent:
        return root / name
    return own


BUILD_TARGETS = ("codex", "claude", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi",
                 "all", "team", "cairn", "vpn", "tor", "browser")  # what `sandbox-ai build NAME` means


def build_command(name):
    """How the variant NAME (or NAME@AGENT) is built: sandbox-ai build NAME, or with --variant when NAME is a build
    target too (a variant of yours called tor: `build tor` builds the Tor VPN's image)."""
    return f"sandbox-ai build --variant {name}" if bare(name) in BUILD_TARGETS else f"sandbox-ai build {name}"


def adopt_variant(name, agent, found=None):
    """The variant NAME of another agent made for AGENT too (its image.json with AGENT, its fragment and files
    copied): a preset or board made with one agent and switched to another names that agent's variant. The other
    agent's that is copied: the first by name. None when there is none to copy, else the agent copied from."""
    found = variants() if found is None else found
    others = [owner for other, owner in found if other == name and owner != agent]
    if not others:
        return None
    source, target = variant_dir(name, others[0]), config_root() / VARIANTS / agent / name
    if target.exists() or target.is_symlink():
        return None
    check_location(config_root(), "The image extensions in")
    document = json.loads(read_limited(source / DOCUMENT, 1 << 20))
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=target.parent))
    try:
        for entry in os.listdir(source):
            path = source / entry
            if entry == FILES and path.is_dir() and not path.is_symlink():
                # The files its image.json installs (images extend --add-file): the copy names them too.
                os.mkdir(staging / FILES, 0o700)
                for name in os.listdir(path):
                    item = path / name
                    if item.is_symlink() or not item.is_file():
                        continue
                    (staging / FILES / name).write_bytes(read_limited(item, FILES_LIMIT))
                    os.chmod(staging / FILES / name, item.stat().st_mode & 0o755)
                continue
            if entry == DOCUMENT or path.is_symlink() or not path.is_file():
                continue
            (staging / entry).write_bytes(read_limited(path, 64 << 20))
            os.chmod(staging / entry, path.stat().st_mode & 0o755)
        (staging / DOCUMENT).write_text(json.dumps({**document, "agent": agent}, indent=2) + "\n")
        os.rename(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return others[0]


def adopted_from(name, agent):
    """The other agent whose variant NAME an adopted one of AGENT copied (said once at the start), or "another
    agent" when it is not known any more."""
    owners = [owner for other, owner in variants() if other == name and owner != agent]
    return owners[0] if owners else "another agent"


def variant_image(agent, name, adopt=False):
    """The image of variant NAME of AGENT (for the launcher's --image-variant), else ValueError. ADOPT: a variant NAME
    of another agent only is made for AGENT too (adopt_variant) instead of refused."""
    if not valid_name(name):
        raise ValueError(f"--image-variant {name!r}: not a variant name")
    found = variants()
    if (name, agent) in found:
        return image_name(name, agent)
    if adopt and adopt_variant(name, agent, found):
        return image_name(name, agent)
    others = [owner for other, owner in found if other == name]
    target = shown_target(name, agent, found + [(name, agent)])
    if name in RECIPES:  # (images new would make an empty one: the recipe brings what the name promises)
        raise ValueError(f"--image-variant {name}: no such variant of {agent}"
                         + (f" (only of {', '.join(others)})" if others else "")
                         + f"; make it from its recipe and build it: sandbox-ai images recipe {name} --from {agent} "
                         f"&& {build_command(target)}")
    if others:
        raise ValueError(f"--image-variant {name} is a variant of {' and '.join(others)}, not of {agent}; make one "
                         f"of {agent} too: sandbox-ai images new {name} --from {agent}")
    raise ValueError(f"--image-variant {name}: no such variant; create it with: sandbox-ai images new {name} "
                     f"--from {agent}")


def load(target, strict=True):
    """(document, fragment text, {file name: bytes}) of TARGET's extension (empty for an agent without one).
    STRICT checks the location first (always before writing or building)."""
    agent = agent_of(target)
    directory = target_dir(target)
    if strict:
        check_location(config_root(), "The image extensions in")
    document, fragment, files = empty_document(agent), "", {}
    if (directory / DOCUMENT).exists() or (directory / DOCUMENT).is_symlink():
        try:
            document = json.loads(read_limited(directory / DOCUMENT, 1 << 20))
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"{directory / DOCUMENT}: {exc}") from None
        if not isinstance(document, dict) or document.get("version") != 1 or document.get("agent") != agent:
            raise ValueError(f"{directory / DOCUMENT}: expected version 1 for {agent}")
        for kind in KINDS:
            values = document.setdefault(kind, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError(f"{directory / DOCUMENT}: {kind} must be a list of strings")
        entries = document.setdefault("files", [])
        if not isinstance(entries, list) or not all(
                isinstance(e, dict) and all(isinstance(e.get(k), str) for k in ("source", "target", "mode"))
                for e in entries):
            raise ValueError(f"{directory / DOCUMENT}: files must be a list of {{source, target, mode}}")
        base = document.get("base")
        if base is not None and (target == agent or not isinstance(base, str) or not re.fullmatch(NAME, base)):
            raise ValueError(f"{directory / DOCUMENT}: base names one of your images (only a variant has one)")
    if (directory / FRAGMENT).exists() or (directory / FRAGMENT).is_symlink():
        fragment = read_limited(directory / FRAGMENT, FRAGMENT_LIMIT * 2).decode("utf-8", "replace")
    folder = directory / FILES
    if folder.is_symlink():
        raise ValueError(f"{folder} is a symbolic link")
    if folder.is_dir():
        total = 0
        for name in sorted(os.listdir(folder))[:FILE_COUNT_LIMIT + 1]:
            if len(files) >= FILE_COUNT_LIMIT:
                raise ValueError(f"{folder}: more than {FILE_COUNT_LIMIT} files")
            files[name] = read_limited(folder / name, FILE_LIMIT)
            total += len(files[name])
            if total > FILES_LIMIT:
                raise ValueError(f"{folder}: more than {FILES_LIMIT >> 20} MiB")
    return document, fragment, files


def has_extension(document, fragment, files=None):
    return any(document.get(kind) for kind in (*KINDS, "files")) or bool(files) or any(
        line.strip() and not line.strip().startswith("#") for line in fragment.splitlines())


# -- the validator --

def finding(rule, line=None, text=None, where="fragment", detail=None, severity=None):
    level, message = RULES[rule]
    return {"severity": severity or level, "rule": rule, "where": where, "line": line, "text": text,
            "message": f"{message}: {detail}" if detail else message}


def logical_lines(text):
    """[(first line, last line, keyword, arguments)] of a Containerfile fragment: comments dropped, continued
    lines (ending in a backslash) joined."""
    result, current, start, terminator = [], None, 0, None
    lines = text.split("\n")
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if terminator is not None:  # the body of a here-document (refused, but not read as instructions)
            if stripped == terminator:
                terminator = None
            continue
        if current is None and (not stripped or stripped.startswith("#")):
            continue
        if current is not None and (not stripped or stripped.startswith("#")):
            continue  # comments and empty lines inside a continued instruction
        if current is None:
            current, start = [], number
        body = line.rstrip()
        if body.endswith("\\"):
            current.append(body[:-1].strip())
            if number < len(lines):
                continue
        else:
            current.append(stripped)
        joined = " ".join(part for part in current if part)
        keyword, _, arguments = joined.partition(" ")
        result.append((start, number, keyword.upper(), arguments.strip()))
        heredoc = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)", arguments)
        if heredoc and keyword.upper() in ("RUN", "COPY", "ADD"):
            terminator = heredoc.group(1)
        current = None
    return result


def json_or_words(arguments):
    """The words of an instruction in JSON form (["a", "b"]) or shell form; ValueError if neither parses."""
    if arguments.startswith("["):
        try:
            words = json.loads(arguments)
        except (ValueError, RecursionError):
            raise ValueError("invalid JSON form") from None
        if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
            raise ValueError("invalid JSON form")
        return words
    try:
        return shlex.split(arguments, posix=True)
    except ValueError as exc:
        raise ValueError(str(exc)) from None


def special_mode(value):
    """MODE (an octal string such as 4755) has setuid or setgid bits."""
    return bool(re.fullmatch(r"[0-7]{1,5}", value)) and int(value, 8) & 0o6000 != 0


def env_names(arguments):
    """The variable names an ENV or ARG line sets."""
    if "=" not in arguments.split(" ", 1)[0]:
        return [arguments.split(" ", 1)[0]]  # legacy form: ENV NAME value
    try:
        words = shlex.split(arguments, posix=True)
    except ValueError:
        words = arguments.split()
    return [word.split("=", 1)[0] for word in words if "=" in word]


def check_target_path(path, where, line=None, rule_masked="masked"):
    """Findings for a file TARGET inside the image."""
    found = []
    if not path.startswith("/"):
        return [finding("file", line, path, where, "the target must be an absolute path")]
    normal = os.path.normpath(path)
    if PROTECTED_RE.search(normal) or PurePosixPath(normal).name in PROTECTED_TOOLS and \
            str(PurePosixPath(normal).parent) in ("/usr/local/bin", "/usr/local/sbin", "/usr/bin", "/usr/sbin",
                                                  "/bin", "/sbin"):
        found.append(finding("protected", line, path, where))
    elif any(normal == m or normal.startswith(m + "/") for m in MASKED):
        found.append(finding(rule_masked, line, path, where))
    return found


def setuid_mode(value):
    """A chmod mode (octal or symbolic) that sets setuid or setgid."""
    return special_mode(value) or bool(re.fullmatch(r"[ugoa+=,rwxXst-]+", value) and
                                       re.search(r"[+=][rwxXt]*s", value))


def command_segments(arguments):
    """The simple commands of a RUN line, as words, without leading variable assignments and sudo/env."""
    for segment in re.split(r"&&|\|\||[;|&\n]", arguments):
        try:
            words = shlex.split(segment, posix=True)
        except ValueError:
            words = segment.split()
        while words and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]) or
                         words[0] in ("sudo", "env", "command", "exec", "nice", "time")):
            words.pop(0)
        if words:
            yield words


def written_paths(words):
    """The paths a simple command (WORDS) writes, renames or removes, as far as the validator knows the tool."""
    tool, rest = PurePosixPath(words[0]).name, words[1:]
    plain = [w for w in rest if not w.startswith("-")]
    if tool in ("ln", "cp", "install", "rsync"):
        return plain[-1:]
    if tool == "sed":
        return plain[1:] if any(w.startswith("-i") or w.startswith("--in-place") for w in rest) else []
    if tool == "dd":
        return [w[3:] for w in rest if w.startswith("of=")]
    if tool in ("rm", "mv", "chmod", "chown", "chgrp", "truncate", "tee", "unlink", "shred", "patch", "chattr",
                "dpkg-divert", "update-alternatives", "rmdir", "mkdir", "touch"):
        return plain
    return []


def check_run(arguments, line, found):
    if re.search(r"<<-?\s*['\"]?[A-Za-z_]", arguments):
        found.append(finding("heredoc", line, "<<"))
    for match in re.finditer(r"\bchmod\s+(?:-[A-Za-z-]+\s+)*([^\s;&|]+)", arguments):
        if setuid_mode(match.group(1)):
            found.append(finding("setuid", line, match.group(1)))
    for match in re.finditer(r"\binstall\b[^;&|]*?(?:\s-m\s*|--mode[= ])([0-7]+)\b", arguments):
        if special_mode(match.group(1)):
            found.append(finding("setuid", line, match.group(1)))
    if re.search(r"\bsetcap\b", arguments):
        found.append(finding("setcap", line, "setcap"))
    for words in command_segments(arguments):
        for path in written_paths(words):
            if PROTECTED_RE.search(os.path.normpath(path) if path.startswith("/") else path):
                found.append(finding("protected", line, path))
                break
    for match in re.finditer(r">>?\s*(" + PROTECTED_RE.pattern + ")", arguments):
        found.append(finding("protected", line, match.group(1)))
    removal = re.search(r"\b(?:apt-get|apt|dpkg)\b[^;&|]*?\s(?:remove|purge|autoremove|-r|-P|--remove|--purge)\b"
                        r"([^;&|]*)", arguments)
    if removal:
        named = [word for word in removal.group(1).split() if word.split(":")[0] in PROTECTED_PACKAGES]
        if named:
            found.append(finding("remove-protected", line, named[0]))
    pipe = re.search(r"\b(?:curl|wget)\b[^;&]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|dash|python3?|perl|node)\b", arguments)
    if pipe:
        found.append(finding("pipe-shell", line, pipe.group(0).split()[0]))
    if re.search(r"\bupdate-ca-certificates\b|/usr/local/share/ca-certificates", arguments):
        found.append(finding("ca-trust", line, "update-ca-certificates" if "update-ca" in arguments
                             else "/usr/local/share/ca-certificates"))


def check_copy(keyword, arguments, line, available, found):
    if re.search(r"<<-?\s*['\"]?[A-Za-z_]", arguments):
        found.append(finding("heredoc", line, "<<"))
        return
    try:
        words = json_or_words(arguments)
    except ValueError as exc:
        found.append(finding("syntax", line, None, detail=str(exc)))
        return
    flags = []
    while words and words[0].startswith("--"):
        flags.append(words.pop(0))
    checksum = False
    for flag in flags:
        name, _, value = flag.partition("=")
        if name == "--from":
            found.append(finding("copy-from", line, flag))
        elif name == "--chmod":
            if setuid_mode(value):
                found.append(finding("setuid", line, flag))
            elif re.fullmatch(r"[0-7]{3,4}", value) and int(value, 8) & 0o002:
                found.append(finding("mode", line, flag))
        elif name == "--checksum" and keyword == "ADD":
            checksum = True
        elif name not in ("--chown", "--link", "--chmod", "--exclude", "--parents", "--keep-git-dir"):
            found.append(finding("unknown-flag", line, flag))
    if len(words) < 2:
        found.append(finding("syntax", line, None, detail=f"{keyword} needs a source and a target"))
        return
    *sources, target = words
    for source in sources:
        if re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", source) or source.startswith("git@"):
            if keyword == "COPY":
                found.append(finding("copy-source", line, source))
            elif source.startswith("git@") or source.endswith(".git") or source.startswith("git://") or \
                    "#" in source:
                found.append(finding("add-git", line, source))
            elif not checksum:
                found.append(finding("add-url", line, source))
            continue
        normal = os.path.normpath(source)
        name = normal[len(FILES) + 1:] if normal.startswith(FILES + "/") else None
        if source.startswith("/") or normal.startswith("..") or not name or "/" in name or \
                any(c in source for c in "*?[") or (available is not None and name not in available):
            found.append(finding("copy-source", line, source))
        elif keyword == "ADD":
            found.append(finding("add-local", line, "ADD"))
    found.extend(check_target_path(target, "fragment", line))


def check_fragment(text, available=None):
    """The findings ({severity, rule, where, line, text, message}) for a Containerfile fragment TEXT.
    AVAILABLE: the names of the extension's files (COPY files/NAME), or None not to check them."""
    found = []
    if len(text.encode("utf-8", "replace")) > FRAGMENT_LIMIT:
        found.append(finding("size"))
    for number, line in enumerate(text.split("\n"), 1):
        bad = next((c for c in line if (ord(c) < 32 and c not in "\t\r") or ord(c) == 127 or
                    0x202a <= ord(c) <= 0x202e or 0x2066 <= ord(c) <= 0x2069), None)
        if bad is not None:
            found.append(finding("control", number, None, detail=f"U+{ord(bad):04X}"))
    lines = text.split("\n")
    for first, last, keyword, arguments in logical_lines(text):
        before = len(found)
        check_instruction(first, keyword, arguments, available, found)
        for item in found[before:]:
            locate(item, lines, first, last)
    return found


def locate(item, lines, first, last):
    """Point ITEM at the line and column of its text within the instruction's lines FIRST..LAST (the editor
    underlines it there); without a text, the instruction's keyword."""
    text = item.get("text")
    for number in range(first, last + 1):
        line = lines[number - 1]
        if text:
            whole = re.search(r"(?<![\w-])" + re.escape(text) + r"(?![\w-])", line)  # PATH, not GOPATH
            column = whole.start() if whole else line.find(text)
        else:
            column = len(line) - len(line.lstrip())
        if text and column < 0 and number == first:
            column = line.upper().find(text.upper())
            if column >= 0:
                item["text"] = text = line[column:column + len(text)]  # as written (cmd for CMD)
        if column >= 0:
            length = len(text) if text else len(line.strip().split(" ")[0]) if line.strip() else 0
            item.update(line=number, column=column, length=length)
            return
    item.update(line=first, column=0, length=len(lines[first - 1]))


def check_instruction(first, keyword, arguments, available, found):
    """Findings for one instruction (KEYWORD ARGUMENTS starting on line FIRST)."""
    if keyword not in ALLOWED:
        rule = {"FROM": "from", "ENTRYPOINT": "entrypoint", "CMD": "entrypoint", "VOLUME": "volume",
                "ONBUILD": "onbuild"}.get(keyword, "unknown")
        found.append(finding(rule, first, keyword if keyword.isascii() else None,
                             detail=None if rule != "unknown" else keyword[:40]))
        return
    if keyword == "RUN":
        check_run(arguments, first, found)
    elif keyword in ("COPY", "ADD"):
        check_copy(keyword, arguments, first, available, found)
    elif keyword in ("ENV", "ARG"):
        for name in env_names(arguments):
            if name in LOADER_ENV:
                found.append(finding("loader", first, name))
            elif name.upper() in WATCHED_ENV:
                found.append(finding("env", first, name))
    elif keyword == "LABEL":
        for match in re.finditer(r"(?:^|\s)\"?(sandbox-ai\.[^=\s\"]*)", arguments):
            found.append(finding("label", first, match.group(1)))
    elif keyword == "USER":
        found.append(finding("user", first, "USER"))
    elif keyword == "WORKDIR":
        found.append(finding("workdir", first, "WORKDIR"))
    elif keyword == "SHELL":
        found.append(finding("shell", first, "SHELL"))
    elif keyword in ("EXPOSE", "HEALTHCHECK", "STOPSIGNAL", "MAINTAINER"):
        found.append(finding("no-effect", first, keyword))


def package_name(kind, value):
    """The name of a package specification without its version."""
    if kind == "apt":
        return value.split("=", 1)[0]
    if kind == "npm":
        head, at, rest = value[1:].partition("@") if value.startswith("@") else ("", "", value)
        if value.startswith("@"):
            return "@" + head
        return rest.split("@", 1)[0]
    match = re.match(r"[A-Za-z0-9._-]+", value)
    return re.sub(r"[-_.]+", "-", match.group(0)).lower() if match else value


def check_package(kind, value, agent=None):
    """None if VALUE is a valid KIND package for the extension, else the reason."""
    if kind not in PACKAGE_PATTERNS:
        return f"unknown kind {kind}"
    if not isinstance(value, str) or not re.fullmatch(PACKAGE_PATTERNS[kind], value):
        return f"{value!r} is not {PACKAGE_HELP[kind]}"
    if kind == "npm" and package_name("npm", value) in AGENT_PACKAGES.values():
        return RULES["agent-package"][1]
    return None


def check_document(document, available=None):
    """Findings for the package lists and files of an extension DOCUMENT."""
    found = []
    agent = document.get("agent")
    for kind in KINDS:
        values = document.get(kind, [])
        if len(values) > PACKAGE_LIMIT:
            found.append(finding("package", None, None, kind, f"more than {PACKAGE_LIMIT} packages"))
        for value in values:
            reason = check_package(kind, value, agent)
            if reason:
                rule = "agent-package" if reason == RULES["agent-package"][1] else "package"
                found.append(finding(rule, None, value, kind, None if rule == "agent-package" else reason))
    for entry in document.get("files", []):
        source, target, mode = entry.get("source", ""), entry.get("target", ""), entry.get("mode", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}", source) or \
                (available is not None and source not in available):
            found.append(finding("file", None, source, "files", "missing from the extension's files"))
        if not re.fullmatch(r"0?[0-7]{3}|[0-7]{4}", mode):
            found.append(finding("file", None, mode, "files", "the mode is an octal number such as 0644"))
        elif special_mode(mode):
            found.append(finding("setuid", None, mode, "files"))
        elif int(mode, 8) & 0o002:
            found.append(finding("mode", None, mode, "files"))
        found.extend(check_target_path(target, "files"))
    return found


def check(document, fragment, files):
    available = set(files)
    return check_document(document, available) + check_fragment(fragment, available)


def errors(findings):
    return [f for f in findings if f["severity"] == "error"]


# -- the Containerfile --

def containerfile(target, document, fragment):
    """The final Containerfile of TARGET's extension: FROM the agent's base image, the package lists, the
    files, your fragment, and the launcher's expectations restored at the end."""
    agent = document["agent"]
    q = shlex.quote
    lines = [f"# Extension {bare(target)} of {agent}: generated by sandbox-ai images (change it with "
             f"sandbox-ai images extend {bare(target)})",
             f"FROM {base_of(target, document)}", "USER root"]
    if document.get("apt"):  # the OS packages: the agent images and their variants are Debian 12 (apt-get)
        lines += os_packages_module().install_lines("debian", document["apt"])
    if document.get("npm"):
        lines += ["RUN npm install --global " + " ".join(q(p) for p in document["npm"]) + " \\",
                  "    && npm cache clean --force"]
    if document.get("pip"):
        lines += ["RUN export UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_DOWNLOADS=never "
                  "UV_NO_CACHE=1 \\"]
        lines += [f"    && uv tool install --python /usr/bin/python3 {q(p)}" +
                  (" \\" if i < len(document["pip"]) - 1 else "") for i, p in enumerate(document["pip"])]
    for entry in document.get("files", []):
        lines.append(f"COPY --chmod={entry['mode']} {FILES}/{entry['source']} {entry['target']}")
    body = fragment.rstrip("\n")
    if body.strip():
        lines += [f"# --- your fragment ({FRAGMENT}) ---", *body.split("\n"), "# --- end of your fragment ---"]
    lines += ["USER root", "WORKDIR /work"]
    return "\n".join(lines) + "\n"


def extension_hash(text, files):
    digest = hashlib.sha256(text.encode())
    for name in sorted(files):
        digest.update(b"\0" + name.encode() + b"\0" + hashlib.sha256(files[name]).digest())
    return digest.hexdigest()


def describe(target, strict=False):
    """Everything about TARGET's extension, as `images extend --show --json` prints it."""
    agent = agent_of(target)
    document, fragment, files = load(target, strict=strict)
    text = containerfile(target, document, fragment)
    findings = check(document, fragment, files)
    shown = target if target in AGENTS else shown_target(bare(target), agent)
    return {"target": shown, "agent": agent, "variant": target != agent, "image": image_name(target, agent),
            "base": base_of(target, document), "base_image": document.get("base"), "path": str(target_dir(target)),
            **{kind: list(document.get(kind, [])) for kind in KINDS}, "files": list(document.get("files", [])),
            "available": sorted(files), "fragment": fragment, "extended": has_extension(document, fragment, files),
            "findings": findings, "containerfile": text, "hash": extension_hash(text, files)}


# -- changes --

def write_private(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)  # (a full disk: no .NAME.PID.tmp left beside the file)
        raise


def remember(action, target, **details):
    """One line in the history of the extensions (changes and builds)."""
    path = history_file()
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "action": action, "target": bare(target),
             **details}
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(json.dumps(entry) + "\n")
        if path.stat().st_size > 1 << 20:
            kept = path.read_text().splitlines()[-2000:]
            write_private(path, ("\n".join(kept) + "\n").encode())
    except OSError as exc:
        print(f"sandbox-ai images: warning: cannot record the history in {path}: {exc}", file=sys.stderr)


def history(target=None):
    try:
        lines = read_limited(history_file(), 4 << 20).decode("utf-8", "replace").splitlines()
    except ValueError:
        return []
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(entry, dict):
            continue
        name, at, agent = str(target).partition("@") if target is not None else ("", "", "")
        if target is None or entry.get("target") == target or \
                (at and entry.get("target") == name and entry.get("agent") in (agent, None)):
            entries.append(entry)
    return entries


def parse_file_option(value):
    """(source path, target, mode) of --add-file SRC:TARGET[:MODE]."""
    parts = value.split(":")
    if len(parts) == 2:
        parts.append("0644")
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"--add-file expects SRC:TARGET[:MODE], e.g. ./tool.sh:/usr/local/bin/tool:0755: {value!r}")
    source, target, mode = parts
    if not re.fullmatch(r"0?[0-7]{3}|[0-7]{4}", mode):
        raise ValueError(f"--add-file: the mode is an octal number such as 0644 or 0755: {mode!r}")
    return source, target, mode.zfill(4)


def save(target, document, fragment, files):
    directory = config_root() / target if target in AGENTS else variant_dir(bare(target), document["agent"])
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(directory / DOCUMENT, (json.dumps(document, indent=2) + "\n").encode())
    if fragment.strip():
        write_private(directory / FRAGMENT, fragment.encode())
    else:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(directory / FRAGMENT)
    folder = directory / FILES
    if files:
        folder.mkdir(mode=0o700, exist_ok=True)
    present = set(os.listdir(folder)) if folder.is_dir() and not folder.is_symlink() else set()
    for name, data in files.items():
        write_private(folder / name, data)
    for name in present - set(files):
        os.unlink(folder / name)


def os_packages_module():
    """os_packages.py: distributions, their package managers and package indexes."""
    global _os_packages
    if _os_packages is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_os_packages", HERE / "os_packages.py")
        with LOADING:
            if _os_packages is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _os_packages = loaded
    return _os_packages


_os_packages = None


def services_module():
    """services.py: your images from registries and Containerfiles (a variant's base)."""
    global _services
    if _services is None:
        spec = importlib.util.spec_from_file_location("services_for_images", HERE / "services.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _services is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                loaded._images = _ThisModule()  # this images module for it (not a second copy)
                _services = loaded
    return _services


class _ThisModule:
    """This module as services.py sees it: its names, as they are now (a test's replacement included)."""

    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name) from None


_services = None


def check_base(target, agent, name):
    """Your image NAME as the base of the variant TARGET: recorded, a Debian 12 image without loader variables
    (services.base_problem); its record, else ValueError."""
    if target == agent:
        raise ValueError(f"the image of {agent} itself keeps sandbox-ai's base; make a variant on yours: sandbox-ai "
                         f"images new NAME --from {agent} --base {name}")
    services = services_module()
    entry = services.record(name)
    problem = services.base_problem(entry)
    if problem:
        raise ValueError(f"your image {name} cannot be a variant's base: {problem}")
    return entry


def base_summary(target):
    """What a start says of the base of the variant TARGET when it is one of your images: {"name", "image", "id",
    "kind", "unchecked", "warning"}; None for a variant on the agent's own base. ValueError when its image is no
    longer recorded."""
    document = load(target, strict=False)[0]
    name = document.get("base")
    if not name:
        return None
    try:
        entry = services_module().record(name)
    except ValueError:
        raise ValueError(f"the variant {target} is built on your image {name}, which is no longer recorded "
                         f"(sandbox-ai images list); give it another base: sandbox-ai images extend {target} "
                         "--base IMAGE (or --no-base), then build it") from None
    return {"name": name, "image": entry["image"], "id": entry["id"], "kind": entry["kind"],
            "unchecked": not entry.get("reviewed"),
            "warning": f"the image variant {target} is built on your image {name} ({entry['image']}), UNCHECKED (not "
                       "reviewed by sandbox-ai): the sandbox's own supervisor - its firewall, connection proxy and "
                       "approvals - runs from that image's programs; a VPN's kill switch (its own container) holds "
                       "regardless"}


# What this preparation holds - against an image that is careless, not against one made to deceive: it runs in the
# image itself, with the image's own shell and apt, so an image that lies (a changed sh, apt or dpkg, a dpkg status
# that claims packages it changed) is not caught here; that is why such a variant stays UNCHECKED and the security
# modes high and paranoid refuse it (modes.py). Against a careless one: no preloaded library; Debian's own
# interpreter, standard library and the libraries its modules load (libssl3, libffi8, zlib1g) and the setup's shell
# and tools from Debian's archive again; no zip of the standard library's name (it stands before the standard library
# on sys.path, even with -I -S); and the sandbox's own processes run /usr/bin/python3 -I -S
# (runtime.SUPERVISOR_PYTHON), so no .pth file, sitecustomize or usercustomize of the image's site-packages runs in
# them. Still the image's: every other program and library on its PATH (nft, firejail, ip ... from the agent's layers
# unless the image already had them), and whatever it changed outside Debian's packages.
PREP_SCRIPT = (
    "set -eu; "
    "if [ -e /etc/ld.so.preload ]; then echo 'sandbox-ai: the image has /etc/ld.so.preload, which loads code into "
    "every program, the setup of the sandbox included' >&2; exit 1; fi; "
    "if [ -e /usr/lib/python311.zip ] || [ -L /usr/lib/python311.zip ]; then echo 'sandbox-ai: the image has "
    "/usr/lib/python311.zip, which Python reads before its standard library' >&2; exit 1; fi; "
    ". /etc/os-release; "
    "if [ \"${ID:-}\" != debian ] || [ \"${VERSION_CODENAME:-}\" != bookworm ]; then echo \"sandbox-ai: the agent's "
    "layers need Debian 12 (bookworm); this image is ${PRETTY_NAME:-unknown}\" >&2; exit 1; fi; "
    "export DEBIAN_FRONTEND=noninteractive; apt-get update; "
    # What the sandbox's setup runs as root, from Debian's archive again (a changed copy in the image is replaced).
    "apt-get install -y --no-install-recommends --reinstall python3-minimal python3 "
    "python3.11-minimal libpython3.11-minimal libpython3.11-stdlib libssl3 libffi8 zlib1g bash util-linux libc6 "
    "ca-certificates; "
    "if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then "
    "apt-get install -y --no-install-recommends nodejs npm; fi; "
    "major=$(node -p 'process.versions.node.split(\".\")[0]'); "
    "if [ \"$major\" -lt 18 ]; then echo \"sandbox-ai: the agents need Node.js 18 or newer; this image has $major\" "
    ">&2; exit 1; fi; "
    "rm -rf /var/lib/apt/lists/*")


def prep_containerfile(target, agent, name, entry):
    """The Containerfile that makes your image NAME (ENTRY: its record) ready for the agent's layers: root, Debian 12,
    no preloaded libraries, the programs the sandbox's setup runs reinstalled from Debian, Node.js 18 or newer."""
    return "\n".join([
        f"# The base of the variant {target} of {agent}: your image {name} ({entry['image']}), UNCHECKED (not reviewed",
        f"# by sandbox-ai). Generated by sandbox-ai images; {build_command(target)} builds the agent's layers on it.",
        f"FROM {entry['image']}", "USER root", f"RUN {PREP_SCRIPT}", "WORKDIR /", ""])


def build_prep(target, podman_url=None, run=subprocess.run, out=lambda text: print(text, file=sys.stderr)):
    """For sandbox-ai build of a variant on a base image of yours: prep_containerfile built (podman build
    --pull=never: nothing is pulled) as prep_image, after checking that the recorded image is still the one Podman
    has under its reference (its ID)."""
    if podman_url:
        raise ValueError("a variant on a base image of yours is built on this machine (your images are recorded here)")
    agent = agent_of(target)
    document = load(target)[0]
    name = document.get("base")
    if not name:
        raise ValueError(f"{target} has no base image of yours")
    entry = check_base(target, agent, name)
    data = inspect(entry["image"], podman_url, run)
    if data is None or str(data.get("Id", "")).removeprefix("sha256:") != entry["id"].removeprefix("sha256:"):
        raise ValueError(f"your image {name} ({entry['image']}) is not what was recorded (missing, or replaced since); "
                         f"add it again: {services_module().add_again(name, entry)}")
    text = prep_containerfile(target, agent, name, entry)
    with tempfile.TemporaryDirectory(prefix="sandbox-ai-prep-") as context:
        (Path(context) / "Containerfile").write_text(text)
        network = ["--network=host"] if os.environ.get("SANDBOX_AI_BUILD_NETWORK") == "host" else []
        out(f"sandbox-ai images: preparing your image {name} for the agent's layers -> {prep_image(target, agent)}")
        argv = podman(podman_url) + ["build", *network, "--pull=never", "--tag", prep_image(target, agent),
                                     "--label", f"sandbox-ai.base-image={name}",
                                     "--label", f"sandbox-ai.base-image-id={entry['id']}",
                                     "--file", str(Path(context) / "Containerfile"), context]
        result = LOG.run("image", f"preparing your image {name} for {target}", argv, runner=run,
                         stdin=subprocess.DEVNULL, stdout=sys.stderr)  # standard output: only the image's name
    if result.returncode != 0:
        remember("prep-failed", target, agent=agent, base=name)
        raise ValueError(f"preparing your image {name} for {agent} failed (exit status {result.returncode}); its "
                         "output above says why")
    remember("prep", target, agent=agent, base=name, image=entry["image"])
    return prep_image(target, agent)


_filelock = None


def changing():
    """The lock of a change to the image extensions (filelock.py, on images/extensions.lock): `images extend`, `new`,
    `recipe` and `remove` run side by side (the app and a terminal) each read an extension and wrote it back whole,
    so one of two packages added at the same moment was lost."""
    global _filelock
    if _filelock is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_filelock_for_images", HERE / "filelock.py")
        with LOADING:
            if _filelock is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _filelock = loaded
    check_location(config_root(), "The image extensions in")  # (before the lock makes the folder)
    return _filelock.locked(config_root() / "extensions")


def under_lock(function):
    """FUNCTION run holding changing()'s lock (reentrant: recipe() calls new_variant() and extend())."""
    def locked(*args, **kwargs):
        with changing():
            return function(*args, **kwargs)
    locked.__name__, locked.__doc__, locked.__wrapped__ = function.__name__, function.__doc__, function
    return locked


@under_lock
def extend(target, add=None, remove=None, add_files=(), remove_files=(), fragment=None, base=None, own_files=()):
    """Change TARGET's extension (an agent's own or a variant's). ADD and REMOVE: {kind: [package]}. BASE: your
    image the variant is built on (a name), "" for the agent's own base again, None to leave it. OWN_FILES:
    [(target path, mode, bytes)] of sandbox-ai's own recipe files (sandbox/recipes), copied in as --add-file does.
    Refused, with nothing written, when the result has errors. Returns (description, changes)."""
    agent = agent_of(target)
    document, old_fragment, files = load(target)
    changes = []
    if base is not None and base != (document.get("base") or ""):
        if base:
            check_base(target, agent, base)
            document["base"] = base
            changes.append(f"base {base}")
        else:
            document.pop("base", None)
            changes.append("base: the agent's own")
    for kind, values in (remove or {}).items():
        for value in values:
            name = package_name(kind, value)
            kept = [p for p in document[kind] if p != value and package_name(kind, p) != name]
            if len(kept) == len(document[kind]):
                raise ValueError(f"{value} is not in the {kind} list of {target}")
            document[kind] = kept
            changes.append(f"-{kind} {value}")
    for kind, values in (add or {}).items():
        for value in values:
            reason = check_package(kind, value, agent)
            if reason:
                raise ValueError(f"--add-{kind}: {reason}")
            name = package_name(kind, value)
            document[kind] = [p for p in document[kind] if package_name(kind, p) != name] + [value]
            changes.append(f"+{kind} {value}")
    for path in remove_files:
        kept = [e for e in document["files"] if e["target"] != path]
        if len(kept) == len(document["files"]):
            raise ValueError(f"no file of {target} goes to {path}")
        document["files"] = kept
        changes.append(f"-file {path}")
    for value in add_files:
        source, path, mode = parse_file_option(value)
        source_path = Path(os.path.abspath(os.path.expanduser(source)))
        check_location(source_path, "--add-file")
        data = read_limited(source_path, FILE_LIMIT)
        name = re.sub(r"[^A-Za-z0-9._+-]", "_", source_path.name).lstrip("._-+") or "file"
        if name in files and files[name] != data:
            raise ValueError(f"a different file named {name} is already in {target}; remove it first "
                             f"(--remove-file) or rename yours")
        files[name] = data
        document["files"] = [e for e in document["files"] if e["target"] != path] + \
            [{"source": name, "target": path, "mode": mode}]
        changes.append(f"+file {path}")
    for path, mode, data in own_files:
        name = PurePosixPath(path).name
        if name in files and files[name] != data:
            raise ValueError(f"a different file named {name} is already in {target}; remove it first "
                             f"(--remove-file)")
        files[name] = data
        document["files"] = [e for e in document["files"] if e["target"] != path] + \
            [{"source": name, "target": path, "mode": mode}]
        changes.append(f"+file {path}")
    new_fragment = old_fragment if fragment is None else fragment
    if fragment is not None and fragment != old_fragment:
        changes.append("fragment")
    # Files no list entry and no COPY line uses any more are dropped.
    used = {e["source"] for e in document["files"]} | set(re.findall(r"(?<![\w./-])" + FILES + r"/([A-Za-z0-9._+-]+)",
                                                                      new_fragment))
    files = {name: data for name, data in files.items() if name in used}
    findings = check(document, new_fragment, files)
    if errors(findings):
        raise ExtensionError(target, findings)
    save(target, document, new_fragment, files)
    if changes:
        remember("extend", target, agent=agent, changes=changes,
                 hash=extension_hash(containerfile(target, document, new_fragment), files))
    return describe(target), changes


class ExtensionError(ValueError):
    def __init__(self, target, findings):
        self.target, self.findings = target, findings
        super().__init__(f"the extension of {target} has errors; nothing was saved")


HYPERFRAMES_FRAGMENT = (
    "# hyperframes 0.8.214 (HeyGen, Apache-2.0) and its 125 npm packages, each pinned by version and SHA-512 in\n"
    "# /opt/hyperframes/package-lock.json (sandbox-ai's review: docs/skills-review.md). npm ci installs exactly\n"
    "# those and runs no install scripts; Chromium and FFmpeg are Debian's, so nothing is downloaded at run time.\n"
    "RUN cd /opt/hyperframes \\\n"
    "    && npm ci --omit=dev --ignore-scripts --no-audit --no-fund \\\n"
    "    && npm cache clean --force \\\n"
    "    && ln -s /opt/hyperframes/node_modules/.bin/hyperframes /usr/local/bin/hyperframes \\\n"
    "    && ln -s /opt/hyperframes/node_modules/.bin/hyperframes-localize-fonts \\\n"
    "       /usr/local/bin/hyperframes-localize-fonts\n"
    "# The browser and encoder it uses (it would download a Chrome of its own otherwise); Hallmark's render tier\n"
    "# finds puppeteer-core here too.\n"
    "ENV HYPERFRAMES_BROWSER_PATH=/usr/bin/chromium HYPERFRAMES_FFMPEG_PATH=/usr/bin/ffmpeg \\\n"
    "    HYPERFRAMES_FFPROBE_PATH=/usr/bin/ffprobe HALLMARK_PUPPETEER=/opt/hyperframes\n")
PLAYWRIGHT_FRAGMENT = (
    "# Playwright 1.64.1 (Microsoft, Apache-2.0) with playwright-core, pinned by version and SHA-512 in\n"
    "# /opt/playwright/package-lock.json; npm ci runs no install scripts. Node finds it from anywhere through\n"
    "# /usr/local/lib/node (one of its global folders): the playwright-skill's run.js and the agent's scripts.\n"
    "# The browser is Debian's Chromium: Playwright installs its own builds from its CDN with no hash to check\n"
    "# them by.\n"
    "RUN cd /opt/playwright \\\n"
    "    && npm ci --omit=dev --ignore-scripts --no-audit --no-fund \\\n"
    "    && npm cache clean --force \\\n"
    "    && mkdir -p /usr/local/lib/node \\\n"
    "    && ln -s /opt/playwright/node_modules/playwright /usr/local/lib/node/playwright \\\n"
    "    && ln -s /opt/playwright/node_modules/playwright-core /usr/local/lib/node/playwright-core \\\n"
    "    && ln -s /opt/playwright/node_modules/.bin/playwright /usr/local/bin/playwright\n"
    "ENV PW_EXECUTABLE_PATH=/usr/bin/chromium PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1\n")

DJANGO_FRAGMENT = (
    "# Django 5.2.18 LTS (BSD-3-Clause) and psycopg 3.3.6 (LGPL-3.0, pure Python on Debian's libpq5) with asgiref,\n"
    "# sqlparse and typing-extensions, each pinned by version and the SHA-256 of its wheel in\n"
    "# /opt/django/requirements.txt: pip installs exactly those wheels (no source build, no other package) into a\n"
    "# venv of their own. TypeScript 6.0.3 (Apache-2.0, no dependencies) pinned by its SHA-512 in\n"
    "# /opt/django/package-lock.json; npm ci runs no install scripts.\n"
    "RUN python3 -m venv /opt/django/venv \\\n"
    "    && /opt/django/venv/bin/pip install --no-cache-dir --require-hashes --no-deps --only-binary :all: -r "
    "/opt/django/requirements.txt \\\n"
    "    && /opt/django/venv/bin/python -c 'import django, psycopg; print(django.get_version(), psycopg.__version__)' \\\n"
    "    && cd /opt/django \\\n"
    "    && npm ci --omit=dev --ignore-scripts --no-audit --no-fund \\\n"
    "    && npm cache clean --force \\\n"
    "    && ln -s /opt/django/venv/bin/django-admin /usr/local/bin/django-admin \\\n"
    "    && ln -s /opt/django/node_modules/.bin/tsc /usr/local/bin/tsc\n")


ANDROID_FRAGMENT = r"""# Android debugging tools (the recipe android). Everything that is not a Debian package of this release is a
# pinned release file with its SHA-256, fetched when the image is built and never at run time:
#   apktool 3.0.3 (2026-07-20), with its own aapt2 2.20 and its android-framework.jar taken out of the jar
#   baksmali and smali 3.0.10 (2026-08-24, the release artifacts of google/smali commit 7a325fc45dc0)
#   jadx 1.5.6 (2026-07-10), the headless CLI
#   Frida 17.25.2 (2026-11-02): the Python bindings from /opt/frida/requirements.txt (pip --require-hashes) and
#   frida-server and frida-gadget for every Android ABI, kept as published (xz) in /opt/frida/android/<abi>
# The same versions as the MCP servers smali and frida of sandbox-ai-mcp, so an agent sees the same tools either way.
ADD --checksum=sha256:dbf930b076c6b9be08d57c449cacefc3bdd6b71ebd59b3066fc0e1f5b14f9423 \
    https://github.com/iBotPeaches/Apktool/releases/download/v3.0.3/apktool_3.0.3.jar /opt/android/apktool.jar
ADD --checksum=sha256:37ae4a41a8886e15c20b8362fa4250f96bbdb55e1a608199ad8b5dff068b588f \
    https://github.com/baksmali/smali/releases/download/3.0.10/baksmali-3.0.10-fat-release.jar /opt/android/baksmali.jar
ADD --checksum=sha256:32fa0e88a6c397b3922201adf5f3e534fbaed5a663c71d0c558c3ddce0af844a \
    https://github.com/baksmali/smali/releases/download/3.0.10/smali-3.0.10-fat-release.jar /opt/android/smali.jar
ADD --checksum=sha256:545ea2be9c242511bc145755cf4bda2485ade42966e096f8b4d3da2a230e8974 \
    https://github.com/skylot/jadx/releases/download/v1.5.6/jadx-1.5.6.zip /opt/android/jadx.zip
ADD --checksum=sha256:7fb906385e3dd99ff11dc3068ee2441cf58ecbb8cf13fbd457db62e0c03d3640 \
    https://github.com/frida/frida/releases/download/17.25.2/frida-server-17.25.2-android-arm.xz /opt/frida/android/arm/frida-server-17.25.2-android-arm.xz
ADD --checksum=sha256:7825e2949268f4a23d38f8aaa3690e3659fd5c730e8062d4913ea0b308b933e1 \
    https://github.com/frida/frida/releases/download/17.25.2/frida-gadget-17.25.2-android-arm.so.xz /opt/frida/android/arm/frida-gadget-17.25.2-android-arm.so.xz
ADD --checksum=sha256:08d7b7f02256621c9089cecd6495a5eb43861bf560a7ed86ad376e75d16a1fb6 \
    https://github.com/frida/frida/releases/download/17.25.2/frida-server-17.25.2-android-arm64.xz /opt/frida/android/arm64/frida-server-17.25.2-android-arm64.xz
ADD --checksum=sha256:8c527f7844932ef0b66d99b38cc7191380e52907ca43c82f2acb6b7801945798 \
    https://github.com/frida/frida/releases/download/17.25.2/frida-gadget-17.25.2-android-arm64.so.xz /opt/frida/android/arm64/frida-gadget-17.25.2-android-arm64.so.xz
ADD --checksum=sha256:9957c81b3c60fbb48774814ea17136efa47f0da0aaac518022d097db41b9e1ba \
    https://github.com/frida/frida/releases/download/17.25.2/frida-server-17.25.2-android-x86.xz /opt/frida/android/x86/frida-server-17.25.2-android-x86.xz
ADD --checksum=sha256:bae97edf0bef867e550a1cb84775edb3461c0bf170138f587a5a668b69d5f2f6 \
    https://github.com/frida/frida/releases/download/17.25.2/frida-gadget-17.25.2-android-x86.so.xz /opt/frida/android/x86/frida-gadget-17.25.2-android-x86.so.xz
ADD --checksum=sha256:ee675076d25599dd55c3b080ccf61c02b44d00653a06db2b0e2a2149ef69dad1 \
    https://github.com/frida/frida/releases/download/17.25.2/frida-server-17.25.2-android-x86_64.xz /opt/frida/android/x86_64/frida-server-17.25.2-android-x86_64.xz
ADD --checksum=sha256:2924dd5ac38b33065223ac747712ee5c83a41d1a8a7fbbc3b292e8600e826a27 \
    https://github.com/frida/frida/releases/download/17.25.2/frida-gadget-17.25.2-android-x86_64.so.xz /opt/frida/android/x86_64/frida-gadget-17.25.2-android-x86_64.so.xz
# apktool's own aapt2 and framework out of its jar (apktool would unpack and chmod +x them at run time, and its
# frame path would be written then: both happen here instead), the smali and jadx launchers, and Frida's bindings in
# a venv of their own. apksigner is Debian's jar, started with the headless JRE (its package would pull in an X11
# JRE through default-jre). Nothing of this is setuid, and nothing fetches anything when the agent runs.
RUN cd /opt/android \
    && unzip -q -j apktool.jar prebuilt/linux/aapt2 prebuilt/android-framework.jar \
    && mkdir -p framework \
    && mv android-framework.jar framework/1.apk \
    && echo "df312db814c018019b2b79a993b041b738d7b76f4a8f28da25bc874a026368cc  aapt2" | sha256sum --check --strict \
    && echo "5dd984016ed5a5eb0eef866e2c6e8cd352e1427828ec23adb18005cf5648f3d7  framework/1.apk" \
       | sha256sum --check --strict \
    && chmod 0755 aapt2 \
    && unzip -q jadx.zip -d jadx \
    && echo "fe3e12c45acf75f92369685fd02d1d7a7323385dc725680a9b98a0dac0ea554b  jadx/lib/jadx-1.5.6-all.jar" \
       | sha256sum --check --strict \
    && rm -f jadx.zip \
    && printf '#!/bin/sh\nexec java -jar /opt/android/apktool.jar --frame-path /opt/android/framework --aapt /opt/android/aapt2 "$@"\n' \
       > /usr/local/bin/apktool \
    && printf '#!/bin/sh\nexec java -jar /opt/android/baksmali.jar "$@"\n' > /usr/local/bin/baksmali \
    && printf '#!/bin/sh\nexec java -jar /opt/android/smali.jar "$@"\n' > /usr/local/bin/smali \
    && printf '#!/bin/sh\nexec java -cp /usr/share/java/apksigner.jar:/usr/share/java/apksig.jar com.android.apksigner.ApkSignerTool "$@"\n' \
       > /usr/local/bin/apksigner \
    && chmod 0755 /usr/local/bin/apktool /usr/local/bin/baksmali /usr/local/bin/smali /usr/local/bin/apksigner \
    && ln -s /opt/android/aapt2 /usr/local/bin/aapt2 \
    && ln -s /opt/android/jadx/bin/jadx /usr/local/bin/jadx \
    && ln -s /opt/android/jadx/bin/jadx-gui /usr/local/bin/jadx-gui \
    && python3 -m venv /opt/frida/venv \
    && /opt/frida/venv/bin/pip install --no-cache-dir --require-hashes --no-deps --only-binary :all: \
       -r /opt/frida/requirements.txt \
    && ln -s /opt/frida/venv/bin/python3 /usr/local/bin/frida-python \
    && chmod -R a-x,a+r /opt/frida/android \
    && apktool --version \
    && baksmali --version \
    && smali --version \
    && jadx --version \
    && apksigner --version \
    && /opt/frida/venv/bin/python -c "import frida; print('frida', frida.__version__)"
"""

# What a project built here finds: the API levels 37, 36 and 35 (platform and build-tools each), one NDK, two CMakes,
# the emulator with one x86_64 system image of API 37 (sdk.lock: every version, size and checksum).
ANDROID_DEV_FRAGMENT = r"""# Android app development (the recipe android-dev, on top of the recipe android). Every download is a row of
# /opt/android-dev/sdk.lock with its size and checksums; install.sh fetches them while the image is built (never at
# run time), stops the build on any difference, unpacks each in place and removes the archive in the same step (no
# layer keeps the 4 GB of archives):
#   the Android SDK in /opt/android-sdk: cmdline-tools 23.0, platform-tools 37.0.1, the platforms android-37.0,
#   android-36 and android-35 with build-tools 37.0.0, 36.0.0 and 35.0.0, the NDK 30.0.16248370 (r30), CMake 4.1.2
#   and 3.22.1, the emulator 37.2.13 and the system image system-images;android-37.0;google_apis;x86_64 (r08)
#   Gradle 9.8.2 in /opt/gradle and the Kotlin compiler 2.4.21 in /opt/kotlinc
# JDK 17 is Debian's (what the Android Gradle plugin needs). By building this image you accept the Android Software
# Development Kit License Agreement (https://developer.android.com/studio/terms): the build says so, and writes the
# acceptance of that one license (licenses/android-sdk-license) that sdkmanager would write, so that the Android Gradle
# plugin may fetch a package a project names and the image lacks. x86_64 only (Google has no Linux arm64 SDK tools).
ENV ANDROID_HOME=/opt/android-sdk ANDROID_SDK_ROOT=/opt/android-sdk \
    ANDROID_NDK_HOME=/opt/android-sdk/ndk/30.0.16248370 ANDROID_NDK_ROOT=/opt/android-sdk/ndk/30.0.16248370 \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 GRADLE_HOME=/opt/gradle KOTLIN_HOME=/opt/kotlinc
RUN sh /opt/android-dev/install.sh /opt/android-dev/sdk.lock /opt/android-dev/package-xml \
    && mkdir -p /opt/android-sdk/licenses \
    && cp /opt/android-dev/android-sdk-license /opt/android-sdk/licenses/android-sdk-license \
    && ln -s /opt/android-sdk/platform-tools/adb /usr/local/bin/adb \
    && ln -s /opt/android-sdk/platform-tools/fastboot /usr/local/bin/fastboot \
    && ln -s /opt/android-sdk/emulator/emulator /usr/local/bin/emulator \
    && ln -s /opt/android-sdk/cmake/4.1.2/bin/cmake /usr/local/bin/cmake \
    && ln -s /opt/android-sdk/cmake/4.1.2/bin/ninja /usr/local/bin/ninja \
    && printf '#!/bin/sh\nexec /opt/android-sdk/cmdline-tools/23.0/bin/sdkmanager "$@"\n' > /usr/local/bin/sdkmanager \
    && printf '#!/bin/sh\nexec /opt/android-sdk/cmdline-tools/23.0/bin/avdmanager "$@"\n' > /usr/local/bin/avdmanager \
    && printf '#!/bin/sh\nexec /opt/android-sdk/ndk/30.0.16248370/ndk-build "$@"\n' > /usr/local/bin/ndk-build \
    && printf '#!/bin/sh\nexec /opt/gradle/bin/gradle "$@"\n' > /usr/local/bin/gradle \
    && printf '#!/bin/sh\nexec /opt/kotlinc/bin/kotlinc "$@"\n' > /usr/local/bin/kotlinc \
    && printf '#!/bin/sh\nexec /opt/kotlinc/bin/kotlin "$@"\n' > /usr/local/bin/kotlin \
    && chmod 0755 /usr/local/bin/sdkmanager /usr/local/bin/avdmanager /usr/local/bin/ndk-build /usr/local/bin/gradle \
       /usr/local/bin/kotlinc /usr/local/bin/kotlin \
    && chmod -R a+rX,go-w /opt/android-sdk /opt/gradle /opt/kotlinc \
    && java -version 2>&1 | grep -q 'version "17\.' \
    && sdkmanager --list_installed > /var/tmp/sdk-installed.txt \
    && for package in $(awk '!/^#/ && NF == 7 && $1 != "-" {print $1}' /opt/android-dev/sdk.lock); do \
         awk -F'|' '{gsub(/ /, "", $1); print $1}' /var/tmp/sdk-installed.txt | grep -qxF "$package" \
         || { echo "sandbox-ai android-dev: ERROR: sdkmanager does not see $package" >&2; \
              cat /var/tmp/sdk-installed.txt >&2; exit 1; }; \
       done \
    && printf 'no\n' | avdmanager --silent create avd --name build-check --device pixel_7 \
       --package 'system-images;android-37.0;google_apis;x86_64' --path /var/tmp/avd-check \
    && for qemu in /opt/android-sdk/emulator/qemu/linux-x86_64/qemu-system-x86_64-headless; do \
         if [ -e "$qemu" ] && LD_LIBRARY_PATH=/opt/android-sdk/emulator/lib64:/opt/android-sdk/emulator/lib64/qt/lib \
              ldd "$qemu" | grep 'not found'; then \
           echo "sandbox-ai android-dev: ERROR: the emulator lacks the libraries above" >&2; exit 1; fi; \
       done \
    && rm -rf /var/tmp/sdk-installed.txt /var/tmp/avd-check /root/.android \
    && adb version \
    && emulator -version \
    && sdkmanager --version \
    && GRADLE_USER_HOME=/var/tmp/gradle-check gradle --version \
    && rm -rf /var/tmp/gradle-check \
    && kotlinc -version \
    && cmake --version \
    && ndk-build --version \
    && echo "sandbox-ai android-dev: by building this image you accepted the Android Software Development Kit License" \
            "Agreement (https://developer.android.com/studio/terms)"
"""
ANDROID_DEV_FILES = [("android-dev/sdk.lock", "/opt/android-dev/sdk.lock", "0644"),
                     ("android-dev/install.sh", "/opt/android-dev/install.sh", "0644"),
                     ("android-dev/android-sdk-license", "/opt/android-dev/android-sdk-license", "0644"),
                     ("android-dev/android-emulator", "/usr/local/bin/android-emulator", "0755")] + [
    (f"android-dev/package-xml/{path.name}", f"/opt/android-dev/package-xml/{path.name}", "0644")
    for path in sorted((HERE / "recipes" / "android-dev" / "package-xml").glob("*.xml"))]

# Ready extensions for a purpose: `sandbox-ai images recipe NAME --from AGENT` makes the variant NAME with them.
# "includes": recipes whose packages, files and fragment come first (android-dev has everything of android).
RECIPES = {
    "media": {"description": "sound, video and speech: ffmpeg, PulseAudio (for --audio virtual), SoX, espeak-ng, "
                             "ImageMagick and scrot for screenshots; Piper (neural text to speech) as a Python tool",
              "apt": ["ffmpeg", "pulseaudio", "pulseaudio-utils", "sox", "espeak-ng", "imagemagick", "scrot",
                      "x11-apps"],
              "pip": ["piper-tts"]},
    # Debian's own packages of the image's release (bookworm: Podman 4.3); start the variant with
    # --nested-containers, which configures Podman for the sandbox (storage in /var/tmp, the agent's network).
    # uidmap's setuid helpers stay without effect (no new privileges); the agent maps no IDs itself.
    "dev": {"description": "for developing sandbox-ai itself with --nested-containers: Podman (crun, conmon, "
                           "fuse-overlayfs, slirp4netns, passt, uidmap), nftables and iproute2 for the kill switch "
                           "tests, tor for the Tor tests, busybox-static for small local test images, Python's venv "
                           "and the libraries the app's tests need offscreen (EGL, GL, xkbcommon, fontconfig, D-Bus, "
                           "Xvfb), spectrwm, xdotool and xwd for the window-manager checks (tests/devtools)",
            "apt": ["podman", "crun", "conmon", "fuse-overlayfs", "slirp4netns", "passt", "uidmap", "nftables",
                    "iproute2", "tor", "git", "python3-venv", "busybox-static", "libegl1", "libgl1",
                    "libxkbcommon0", "libfontconfig1", "libdbus-1-3", "xvfb", "spectrwm", "xdotool", "x11-utils",
                    "x11-apps"]},
    # The CLIs of the built-in kits' skills (sandbox/builtin/kits, docs/skills-review.md), installed when the image
    # is built from sandbox-ai's own lockfiles (sandbox/recipes/NAME): every npm package pinned by its SHA-512, no
    # install scripts, the browsers Debian's - a sandbox then downloads nothing for them.
    "hyperframes": {"description": "for the kit hyperframes (video from HTML): the hyperframes CLI 0.8.214 with its "
                                   "npm packages pinned by their SHA-512 (npm ci, no install scripts), Chromium and "
                                   "FFmpeg from Debian; also gives the kit hallmark its render tier (puppeteer-core)",
                    "apt": ["chromium", "ffmpeg", "fonts-liberation"],
                    "files": [("hyperframes/package.json", "/opt/hyperframes/package.json", "0644"),
                              ("hyperframes/package-lock.json", "/opt/hyperframes/package-lock.json", "0644")],
                    "fragment": HYPERFRAMES_FRAGMENT},
    "playwright": {"description": "for the kit playwright-skill: Playwright 1.64.1 pinned by its SHA-512 (npm ci, no "
                                  "install scripts), found by Node from every folder, and Debian's Chromium "
                                  "(PW_EXECUTABLE_PATH) instead of a browser download",
                   "apt": ["chromium", "fonts-liberation"],
                   "files": [("playwright/package.json", "/opt/playwright/package.json", "0644"),
                             ("playwright/package-lock.json", "/opt/playwright/package-lock.json", "0644")],
                   "fragment": PLAYWRIGHT_FRAGMENT},
    # Android debugging: the device tools of Debian's release, and the analysis tools pinned by their SHA-256 (the
    # versions sandbox-ai-mcp's smali and frida servers use). Start the sandbox with --allow-host-port 5037 to reach
    # an adb server on this host, and give it the device with --device where you hand one over directly.
    "android": {"description": "for Android debugging (the preset android-debugging): adb and fastboot, aapt, "
                               "zipalign and apksigner from Debian, apktool 3.0.3 with its own aapt2, baksmali and "
                               "smali 3.0.10, jadx 1.5.6 and Frida 17.25.2's Python bindings - each pinned by its "
                               "SHA-256 - with frida-server and frida-gadget for every Android ABI in "
                               "/opt/frida/android (about 140 MB, as published); frida-tools is not included "
                               "(sandbox/recipes/android/requirements.txt says why)",
                "apt": ["adb", "fastboot", "aapt", "zipalign", "libapksig-java", "android-sdk-platform-tools-common",
                        "default-jre-headless", "unzip", "xz-utils", "python3-venv", "sqlite3"],
                "files": [("android/requirements.txt", "/opt/frida/requirements.txt", "0644")],
                "fragment": ANDROID_FRAGMENT},
    # Android app development (the preset android-development): everything of android, the Android SDK for the API
    # levels 37, 36 and 35, the NDK, CMake, the emulator with an x86_64 system image, Gradle and Kotlin - every
    # download pinned in sandbox/recipes/android-dev/sdk.lock (size, Google's SHA-1, SHA-256) and fetched only while
    # the image is built. The emulator's libraries are Debian's (the emulator brings its own Qt).
    "android-dev": {"description": "for Android app development (the preset android-development): everything of "
                                   "the recipe android, JDK 17 from Debian, the Android SDK in /opt/android-sdk - "
                                   "cmdline-tools 23.0, platform-tools 37.0.1, the platforms and build-tools of the "
                                   "API levels 37, 36 and 35, the NDK r30 (30.0.16248370), CMake 4.1.2 and 3.22.1, "
                                   "the emulator 37.2.13 with the system image android-37.0 google_apis x86_64 - "
                                   "Gradle 9.8.2 and the Kotlin compiler 2.4.21; every download pinned by its size "
                                   "and checksum (sandbox/recipes/android-dev/sdk.lock), about 4.1 GB to fetch and "
                                   "roughly 10 GB more in the image; x86_64 only. By building it you accept the "
                                   "Android Software Development Kit License Agreement "
                                   "(https://developer.android.com/studio/terms)",
                    "includes": ["android"],
                    "apt": ["openjdk-17-jdk-headless", "curl", "ca-certificates", "unzip", "libpulse0", "libnss3",
                            "libnspr4", "libxcomposite1", "libxcursor1", "libxdamage1", "libxi6", "libxtst6",
                            "libxrandr2", "libasound2", "libgl1", "libegl1", "libxkbfile1", "libdbus-1-3",
                            "libx11-xcb1", "libxkbcommon-x11-0", "libfontconfig1", "libtinfo5", "libbsd0",
                            "libxcb-cursor0", "libxcb-icccm4", "libxcb-image0", "libxcb-keysyms1", "libxcb-randr0",
                            "libxcb-render-util0", "libxcb-shape0", "libxcb-xfixes0", "libxcb-xkb1"],
                    "files": ANDROID_DEV_FILES,
                    "fragment": ANDROID_DEV_FRAGMENT},
    # The development image of the showcase preset showcase-full-stack: Python, JavaScript and Django on the agent's
    # own Python 3.11 and Node.js, from sandbox-ai's lockfiles (sandbox/recipes/django), psql for the --service db.
    "django": {"description": "for Python and JavaScript web development with Django: Django 5.2.18 LTS and psycopg "
                              "3.3.6 in /opt/django/venv (every wheel pinned by its SHA-256, pip --require-hashes), "
                              "TypeScript 6.0.3 pinned by its SHA-512 (npm ci, no install scripts), psql and libpq "
                              "from Debian",
               "apt": ["postgresql-client", "libpq5"],
               "files": [("django/requirements.txt", "/opt/django/requirements.txt", "0644"),
                         ("django/package.json", "/opt/django/package.json", "0644"),
                         ("django/package-lock.json", "/opt/django/package-lock.json", "0644")],
               "fragment": DJANGO_FRAGMENT},
}
RECIPE_FILES = HERE / "recipes"  # the files the recipes copy into the image (part of sandbox-ai, read-only)


def recipe_parts(name, seen=()):
    """RECIPES[NAME] with what it includes put first: {kind: [packages]} (each once, in order), "files" and the
    "fragment" (the included recipes' fragments, then its own)."""
    if name not in RECIPES:
        raise ValueError(f"no recipe {name!r}; recipes: {', '.join(sorted(RECIPES))}")
    if name in seen:
        raise ValueError(f"the recipe {name} includes itself")
    wanted = RECIPES[name]
    parts = {kind: [] for kind in KINDS}
    parts.update(files=[], fragment="")
    for included in [*wanted.get("includes", []), None]:
        source = recipe_parts(included, (*seen, name)) if included else wanted
        for kind in KINDS:
            parts[kind] += [p for p in source.get(kind, []) if p not in parts[kind]]
        parts["files"] += [entry for entry in source.get("files", []) if entry not in parts["files"]]
        if source.get("fragment"):
            parts["fragment"] += ("\n" if parts["fragment"] else "") + source["fragment"]
    return parts


@under_lock
def recipe(name, agent, variant=None):
    """The variant VARIANT (default NAME) of AGENT with the recipe NAME's packages; build it with sandbox-ai build."""
    if name not in RECIPES:
        raise ValueError(f"no recipe {name!r}; recipes: {', '.join(sorted(RECIPES))}")
    variant = variant or name
    wanted = recipe_parts(name)
    new_variant(variant, agent)
    variant = f"{variant}@{agent}"  # (the same name may be a variant of another agent too)
    fragment = None
    if wanted.get("fragment"):  # after the agent's own fragment, which the variant copied
        own = load(variant)[1].rstrip("\n")
        fragment = (own + "\n" if own.strip() else "") + wanted["fragment"]
    own_files = [(target, mode, read_limited(RECIPE_FILES / source, FILE_LIMIT))
                 for source, target, mode in wanted.get("files", [])]
    return extend(variant, add={kind: list(wanted.get(kind, [])) for kind in KINDS if wanted.get(kind)},
                  fragment=fragment, own_files=own_files)


@under_lock
def new_variant(name, agent, empty=False, base=None):
    """The variant NAME of AGENT: its extension copied from the agent's (unless EMPTY); BASE: one of your images
    (sandbox-ai images pull|build-image) it is built on instead of the agent's own base."""
    if not taggable(name):
        raise ValueError(f"{name!r}: a variant name is {NAME_RULE}, and not one of {', '.join(sorted(RESERVED))}")
    if agent not in AGENTS:
        raise ValueError(f"--from expects one of {', '.join(AGENTS)}")
    check_location(config_root(), "The image extensions in")
    directory = config_root() / VARIANTS / agent / name
    if directory.exists() or directory.is_symlink() or (name, agent) in variants():
        raise ValueError(f"{name} of {agent} exists already ({directory})")
    if name in services_module().read_registry():
        raise ValueError(f"{name} is the name of one of your images (sandbox-ai images list); choose another one")
    if base:
        check_base(name, agent, base)
    document, fragment, files = (empty_document(agent), "", {}) if empty else load(agent)
    if base:
        document = {**document, "base": base}
    findings = check(document, fragment, files)
    if errors(findings):
        raise ExtensionError(agent, findings)
    save(name, document, fragment, files)
    remember("new", name, agent=agent, copied=not empty, **({"base": base} if base else {}))
    return describe(f"{name}@{agent}")


def remove(target, keep_image=False, podman_url=None, run=subprocess.run):
    """Drop TARGET's extension; for a variant also its image (unless KEEP_IMAGE). Returns notes."""
    agent = agent_of(target)
    check_location(config_root(), "The image extensions in")
    directory = target_dir(target)
    notes = []
    try:
        based = target != agent and bool(load(target, strict=False)[0].get("base"))
    except ValueError:
        based = False
    with changing():  # (not around Podman's image rm below: a slow Podman would hold every other change up)
        if directory.is_symlink():
            os.unlink(directory)
        elif directory.exists():
            shutil.rmtree(directory)
    if target == agent:
        notes.append(f"the next sandbox-ai build {agent} leaves the extension out")
    elif not keep_image:
        # A variant on a base of yours has its own base layers (and their preparation) too.
        for image in [image_name(target, agent)] + ([f"localhost/{agent}-sandbox-{bare(target)}:base",
                                                     prep_image(target, agent)] if based else []):
            result = run(podman(podman_url) + ["image", "rm", image], capture_output=True,
                         text=True, timeout=120, stdin=subprocess.DEVNULL)
            if result.returncode != 0 and "image not known" not in (result.stderr or "") and \
                    image == image_name(target, agent):
                notes.append(f"the image {image} stays: {(result.stderr or '').strip()[-300:]}")
    remember("remove", target, agent=agent)
    return notes


# -- Podman --

def podman(podman_url=None):
    """The podman command line; without a systemd user session (no user bus) with --cgroup-manager=cgroupfs, as
    sandbox-ai build builds then (a build's RUN steps would ask systemd for their cgroup and fail)."""
    if podman_url:
        return ["podman", "--remote", "--url", podman_url]
    return ["podman", *([] if user_bus() else ["--cgroup-manager=cgroupfs"])]


def user_bus(uid=None):
    """True if this user's systemd user session has its bus socket (rootless Podman's systemd cgroup manager needs
    it): doctor's test, which sandbox-ai build's matches."""
    global _doctor
    if _doctor is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_doctor_for_images", HERE / "doctor.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _doctor is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _doctor = loaded
    return _doctor.user_bus(uid) is not None


_doctor = None


def inspect(name, podman_url=None, run=subprocess.run):
    """The inspect document of image NAME, or None if it is not there."""
    result = LOG.run("image", f"inspect the image {name}", podman(podman_url) + ["image", "inspect", "--format", "json",
                                                                                 name],
                     runner=run, ok=(0, 1, 125), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        LOG.debug("image", f"{name} is not there (podman image inspect: exit status {result.returncode})")
        return None
    try:
        data = json.loads(result.stdout)[0]
        return data if isinstance(data, dict) else None
    except (ValueError, IndexError, KeyError, TypeError, RecursionError):
        return None


def labels_of(data):
    return (data.get("Config") or {}).get("Labels") or data.get("Labels") or {}


def images(podman_url=None, run=subprocess.run):
    """[{agent, target, variant, image, present, id, created, size, agent_version, extended, extension_hash,
    built_hash, status}] of the agents' images and the variants. STATUS: current, outdated (the extension
    changed since the build), base-changed (the base image was rebuilt since), invalid (the extension has
    errors), not-built."""
    found, bases = [], {}
    for target, agent in targets():
        name = image_name(target, agent)
        row = {"agent": agent, "target": target, "variant": target != agent, "image": name}
        try:
            document, fragment, files = load(target, strict=False)
            if document.get("base"):  # a variant on one of your images (unchecked)
                row["base_image"] = document["base"]
            extended = has_extension(document, fragment, files) or target != agent
            wanted = extension_hash(containerfile(target, document, fragment), files) if extended else None
            broken = bool(errors(check(document, fragment, files)))
        except ValueError as exc:
            extended, wanted, broken = True, None, True
            row["extension_error"] = str(exc)
        row.update(extended=extended, extension_hash=wanted)
        data = inspect(name, podman_url, run)
        if data is None:
            row.update(present=False, built_hash=None, status="invalid" if broken else "not-built")
            found.append(row)
            continue
        labels = labels_of(data)
        row.update(present=True, id=str(data.get("Id", ""))[:12], created=str(data.get("Created", ""))[:19],
                   size=int(data.get("Size") or 0), agent_version=labels.get(LABEL),
                   built_hash=labels.get(HASH_LABEL))
        if broken:
            status = "invalid"
        elif row["built_hash"] != wanted:
            status = "outdated"
        else:
            status = "current"
            if labels.get(BASE_LABEL):
                below = base_of(target, document) if not row.get("extension_error") else base_image(agent)
                if below not in bases:
                    base = inspect(below, podman_url, run)
                    bases[below] = str(base.get("Id", "")) if base else None
                if bases[below] and bases[below] != labels[BASE_LABEL]:
                    status = "base-changed"
        row["status"] = status
        found.append(row)
    return found


PACKAGES_SCRIPT = ("dpkg-query -W -f '${Package}\\t${Version}\\n' 2>/dev/null | sed 's/^/deb\\t/'; "
                   "npm ls -g --depth 0 --parseable --long 2>/dev/null | sed -n 's#.*/node_modules/\\(.*\\):\\(.*\\)@\\(.*\\):.*#npm\\t\\2\\t\\3#p'; "
                   "for tool in uv gh git python3 node; do v=$($tool --version 2>/dev/null | head -1); "
                   "[ -n \"$v\" ] && printf 'tool\\t%s\\t%s\\n' \"$tool\" \"$v\"; done; "
                   "UV_TOOL_DIR=/opt/uv-tools uv tool list 2>/dev/null | sed -n 's/^\\([^ -][^ ]*\\) v\\(.*\\)$/pip\\t\\1\\t\\2/p'")


def packages(agent, podman_url=None, run=subprocess.run):
    """[(kind, name, version)] installed in the image of AGENT or a variant (deb, npm, pip, tool), read by
    running it offline."""
    try:
        owner = agent_of(agent)
    except ValueError:
        raise ValueError(f"agent must be one of {', '.join(AGENTS)} or a variant") from None
    image = image_name(agent, owner)
    result = run(podman(podman_url) + ["run", "--rm", "--pull=never", "--network=none", "--read-only",
                                       "--cap-drop=ALL", "--security-opt=no-new-privileges", "--entrypoint=/bin/sh",
                                       image, "-c", PACKAGES_SCRIPT],
                 capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise ValueError(f"cannot read the image of {agent}: {result.stderr.strip()[-300:] or 'not built'}"
                         f" (sandbox-ai build {agent})")
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] in ("deb", "npm", "pip", "tool"):
            rows.append(tuple(parts))
    return rows


# -- the OS packages of an image: its distribution, a search in its package index, your images with more --

def is_target(name):
    """NAME is an agent or a variant (else it may be one of your images)."""
    try:
        agent_of(name)
        return True
    except ValueError:
        return False


def distribution(name):
    """What the OS packages of NAME install with - an agent's image or variant (Debian 12; a variant on one of your
    images: that image's), or one of your images (its /etc/os-release, recorded when it was added):
    {name, kind, release, family, label, manager, system}; family None for a distribution sandbox-ai does not know."""
    osp = os_packages_module()
    if is_target(name):
        document = load(name, strict=False)[0]
        base = document.get("base") if name not in AGENTS else None
        release = (services_module().record(base).get("os") or {}) if base else dict(osp.DEBIAN_12)
        kind = "variant" if name not in AGENTS else "agent"
    else:
        release, kind = services_module().record(name).get("os") or {}, "image"
    return {"name": name, "kind": kind, "release": release, **osp.describe(release)}


def search_image(name, podman_url=None, run=subprocess.run):
    """(image, its inspect document) a search of NAME's packages runs: an agent's or variant's image, else its base
    (the same repositories), or your image; ValueError when none is there."""
    if is_target(name):
        agent = agent_of(name)
        document = load(name, strict=False)[0]
        candidates = [image_name(name, agent), base_of(name, document)]
        missing = f"{name} is not built yet ({build_command(name) if name not in AGENTS else 'sandbox-ai build ' + name})"
    else:
        candidates = [services_module().record(name)["image"]]
        missing = f"your image {name} is missing from Podman (add it again: sandbox-ai images pull|build-image --replace)"
    for image in candidates:
        data = inspect(image, podman_url, run)
        if data is not None:
            return image, data
    raise ValueError(f"{missing}: the search reads the package index in a container of it")


class IndexUnavailable(ValueError):
    """The package index could not be fetched (no network, or no tool that lists it)."""


def fetch_index(image, family, podman_url=None, run=subprocess.run):
    """[(name, version, summary)] of IMAGE's package repositories, read in a throwaway container of it with the
    network an extension's build has (Podman's own; the host's with SANDBOX_AI_BUILD_NETWORK=host, as sandbox-ai
    build found). The container is removed whatever happens (a cancelled search too)."""
    osp = os_packages_module()
    name = f"sandbox-ai-package-index-{os.getpid()}-{int(time.time() * 1000) % 100000}"
    network = ["--network=host"] if os.environ.get("SANDBOX_AI_BUILD_NETWORK") == "host" else []
    argv = podman(podman_url) + ["run", "--rm", f"--name={name}", "--pull=never", *network, "--user=0:0",
                                 "--entrypoint=/bin/sh", image, "-c", osp.INDEX_SCRIPTS[family]]
    done = False
    try:
        LOG.info("image", f"reading the package index of {image} ({osp.FAMILIES[family]['manager']})")
        result = run(argv, capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)
        done = True
    finally:
        if not done:  # cancelled or timed out: no container of it stays behind
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                run(podman(podman_url) + ["rm", "--force", "--time", "0", "--ignore", name], capture_output=True,
                    text=True, timeout=60, stdin=subprocess.DEVNULL)
    said = (result.stderr or "").strip()[-300:]
    if result.returncode == 3:
        raise IndexUnavailable(f"the package index of {image} could not be fetched - no network? (the search uses "
                               f"the network an extension's build has: {osp.FAMILIES[family]['manager']} reaches the "
                               f"distribution's mirrors from this host){': ' + said if said else ''}")
    if result.returncode == 4:
        raise IndexUnavailable(f"{image}: {osp.NO_LISTING.get(family, 'no tool lists its packages')}")
    if result.returncode != 0:
        raise ValueError(f"cannot read the package index of {image} (exit status {result.returncode})"
                         f"{': ' + said if said else ''}")
    if len(result.stdout or "") > osp.OUTPUT_LIMIT:
        raise ValueError(f"the package index of {image} is larger than {osp.OUTPUT_LIMIT >> 20} MB")
    rows = osp.parse_index(family, result.stdout or "")
    if not rows:
        raise IndexUnavailable(f"the package index of {image} is empty - no network? ({said or 'nothing listed'})")
    return rows


def package_search(name, query, refresh=False, limit=50, podman_url=None, run=subprocess.run, now=time.time):
    """The OS packages of NAME's distribution that match QUERY: {name, image, family, manager, system, indexed,
    count, results: [{name, version, summary}], stale?}. The index is read once per image (by its ID) and cached
    (~/.cache/sandbox-ai/package-index), again after a week or with REFRESH; without network an index cached before
    is used (STALE says why), else ValueError."""
    osp = os_packages_module()
    found = distribution(name)
    family = found["family"]
    if family is None:
        raise ValueError(f"{name} is {found['system']}: sandbox-ai knows the packages of Debian/Ubuntu (apt), Alpine "
                         "(apk), Fedora/RHEL (dnf), Arch Linux (pacman) and openSUSE (zypper); type the package's name")
    image, data = search_image(name, podman_url, run)
    image_id = str(data.get("Id") or image)
    cached = osp.load_index(family, image_id)
    stale = None
    if refresh or cached is None or now() - int(cached.get("time") or 0) > osp.INDEX_AGE:
        try:
            cached = osp.save_index(family, image_id, image, fetch_index(image, family, podman_url, run))
        except IndexUnavailable as exc:
            if cached is None:
                raise
            stale = f"{exc}; the index read on {time.strftime('%Y-%m-%d', time.localtime(cached['time']))} is used"
    limit = max(1, min(int(limit), osp.RESULT_LIMIT))
    return {"name": name, "image": image, "id": image_id[:19], "family": family, "label": found["label"],
            "manager": found["manager"], "system": found["system"], "indexed": cached.get("time"),
            "count": len(cached["rows"]), "query": query,
            "results": [{"name": n, "version": v, "summary": s} for n, v, s in osp.search(cached["rows"], query, limit)],
            **({"stale": stale} if stale else {})}


def derive_containerfile(source, entry, family, packages):
    """The Containerfile of your image SOURCE (its record ENTRY) with PACKAGES installed by FAMILY's manager, its
    own user again after."""
    osp = os_packages_module()
    user = str(entry.get("config_user") or "")
    return "\n".join([
        f"# Your image {source} ({entry['image']}) with {osp.FAMILIES[family]['label']} packages: generated by "
        "sandbox-ai images derive (UNCHECKED, as the image)",
        f"FROM {entry['image']}", "USER root", *osp.install_lines(family, packages),
        *([f"USER {user}"] if user and user not in ("root", "0", "0:0") and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", user)
          else []), ""])


def derive(name, source, packages, replace=False, run=None, out=print, npm=(), pip=()):
    """Your image NAME: your image SOURCE with the OS PACKAGES installed by its own distribution's manager (apk on
    Alpine, dnf on Fedora ...), built with the network an extension's build has, pinned and recorded UNCHECKED as
    the image it comes from. NAME may be SOURCE itself (with REPLACE). Returns (name, record)."""
    osp, services = os_packages_module(), services_module()
    if npm or pip:
        raise ValueError("--add-npm/--add-pip: only the agent images and their variants (Debian 12, with Node.js and "
                         "uv) get npm packages and Python tools; for your image take its distribution's own (Alpine: "
                         "nodejs npm, py3-NAME; Fedora: nodejs, python3-NAME) with --add-os, or write a Containerfile "
                         "(sandbox-ai images build-image). On Alpine (musl libc) many wheels from PyPI, built for "
                         "glibc, do not install at all")
    entry = services.record(source)
    found = osp.describe(entry.get("os") or {})
    family = found["family"]
    if family is None:
        raise ValueError(f"your image {source} is {found['system']}: sandbox-ai knows the package managers of "
                         "Debian/Ubuntu (apt), Alpine (apk), Fedora/RHEL (dnf), Arch Linux (pacman) and openSUSE "
                         "(zypper); for another one write a Containerfile (sandbox-ai images build-image)")
    if not packages:
        raise ValueError("name the packages to add: --add-os PKG (repeatable)")
    for value in packages:
        reason = osp.check_name(family, value)
        if reason:
            raise ValueError(f"--add-os: {reason}")
    text = derive_containerfile(source, entry, family, packages)
    with tempfile.TemporaryDirectory(prefix="sandbox-ai-derive-") as folder:
        path = Path(folder) / "Containerfile"
        path.write_text(text)
        out(f"sandbox-ai images: {name} = {source} + {found['manager']} {' '.join(packages)}")
        return services.build(name, path, context=folder, replace=replace, run=run, out=out, network=True,
                              derived={"from": source, "family": family, "packages": list(packages)})


def build_plan(target):
    """(agent, kind, number of variants) for sandbox-ai build: KIND is "variant", "extended" (the agent's
    image gets an extension layer) or "plain". Errors in the extension stop the build before it starts."""
    agent = agent_of(target)
    document, fragment, files = load(target)
    found = check(document, fragment, files)
    if errors(found):
        raise ExtensionError(target, found)
    if target != agent:
        if document.get("base"):  # built on one of your images: its own base layers first (build_prep)
            check_base(target, agent, document["base"])
            return agent, "based", 0
        return agent, "variant", 0
    count = sum(1 for _, owner in variants() if owner == agent)
    return agent, "extended" if has_extension(document, fragment, files) else "plain", count


def build_layer(target, base_id, podman_url=None, run=subprocess.run):
    """Build TARGET's extension layer on the agent's base image (podman build --pull=never), labelled with the
    extension's hash and the base's ID. The build context is a fresh private folder holding only the
    Containerfile and the extension's files, as they were read and hashed."""
    agent = agent_of(target)
    document, fragment, files = load(target)
    found = check(document, fragment, files)
    if errors(found):
        raise ExtensionError(target, found)
    if document.get("base"):  # on its own base (the agent's layers on your image): that one's ID
        own = inspect(base_of(target, document), podman_url, run)
        if own is None:
            raise ValueError(f"no base image {base_of(target, document)}: run {build_command(target)}")
        base_id = str(own.get("Id", ""))
    text = containerfile(target, document, fragment)
    digest = extension_hash(text, files)
    with tempfile.TemporaryDirectory(prefix="sandbox-ai-image-") as context:
        context = Path(context)
        (context / "Containerfile").write_text(text)
        (context / FILES).mkdir()
        for name, data in files.items():
            (context / FILES / name).write_bytes(data)
        # sandbox-ai build found that only the host's network resolves names (build_image.sh): the same here.
        network = ["--network=host"] if os.environ.get("SANDBOX_AI_BUILD_NETWORK") == "host" else []
        argv = podman(podman_url) + ["build", *network, "--pull=never", "--tag", image_name(target, agent), "--file",
                                     str(context / "Containerfile"), "--label", f"{EXTENSION_LABEL}={bare(target)}",
                                     "--label", f"{HASH_LABEL}={digest}", "--label", f"{BASE_LABEL}={base_id}",
                                     str(context)]
        if run is subprocess.run:  # its steps said as they go, bounded (progress.py; SANDBOX_AI_PODMAN_OUTPUT=full)
            argv = [sys.executable, "-I", str(HERE / "progress.py"), "podman", *argv]
        LOG.info("image", f"building the extension {target} -> {image_name(target, agent)}")
        result = LOG.run("image", f"podman build of the extension {target}", argv, runner=run, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        LOG.warning("image", f"the extension {target} was not built (exit status {result.returncode}; its output on "
                             "standard error)")
        remember("build-failed", target, agent=agent, hash=digest)
        raise ValueError(f"podman build of the extension {target} failed (exit status {result.returncode})")
    LOG.info("image", f"built the extension {target} as {image_name(target, agent)}")
    remember("build", target, agent=agent, hash=digest, base=base_id[:12], image=image_name(target, agent))
    return digest


def build_layers(target, with_variants=False, podman_url=None, run=subprocess.run, out=print):
    """The layers sandbox-ai build adds after the base image of TARGET's agent: the agent's own extension
    (or, without one, the base tagged as the agent's image) and, WITH_VARIANTS, every variant of the agent;
    for a variant only that one. 0 if all were built, else 1 (the others are built anyway)."""
    agent = agent_of(target)
    # A variant on a base image of yours has its own base (build_layer reads it): the agent's is not needed.
    own_base = target != agent and bool(load(target, strict=False)[0].get("base"))
    base = None if own_base else inspect(base_image(agent), podman_url, run)
    if base is None and not own_base:
        raise ValueError(f"no base image {base_image(agent)}: run sandbox-ai build {agent}")
    base_id = str(base.get("Id", "")) if base else ""
    jobs = [target] if target != agent else [agent] + ([f"{n}@{a}" for n, a in variants() if a == agent] if with_variants else [])
    failed = 0
    for job in jobs:
        if job != target and job != agent:
            try:
                based = load(job, strict=False)[0].get("base")
            except ValueError:
                based = None
            if based:  # its own base layers come from its own build, not from the agent's
                out(f"sandbox-ai images: {job} is built on your image {based}: {build_command(job)} builds it")
                continue
        try:
            if job == agent:
                document, fragment, files = load(agent)
                if not has_extension(document, fragment, files):
                    result = run(podman(podman_url) + ["tag", base_image(agent), image_name(agent, agent)],
                                 capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
                    if result.returncode != 0:
                        raise ValueError(f"podman tag failed: {(result.stderr or '').strip()[-300:]}")
                    continue
            out(f"sandbox-ai images: building the extension {job} -> {image_name(job, agent)}")
            build_layer(job, base_id, podman_url, run)
        except ExtensionError as exc:
            failed += 1
            LOG.warning("image", f"{job}: its extension has errors, not built", exc)
            print(f"sandbox-ai images: {exc}", file=sys.stderr)
            for item in errors(exc.findings):
                print(f"  {format_finding(item)}", file=sys.stderr)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            failed += 1
            LOG.warning("image", f"{job}: not built", exc)
            print(f"sandbox-ai images: {job}: {exc}", file=sys.stderr)
    return 1 if failed else 0


# -- the command --

def format_finding(item):
    where = f"line {item['line']}" if item.get("line") else item.get("where", "")
    text = f" ({item['text']})" if item.get("text") else ""
    return f"{item['severity']}: {where}{text}: {item['message']}"


def read_fragment(value, stdin=None):
    if value == "-":
        data = (stdin or sys.stdin.buffer).read(FRAGMENT_LIMIT * 2 + 1)
        return data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    check_location(value, "--fragment")
    return read_limited(Path(value).expanduser(), FRAGMENT_LIMIT * 2).decode("utf-8", "replace")


STATUS_TEXT = {"current": "current", "outdated": "extension changed: rebuild", "base-changed": "base rebuilt: rebuild",
               "invalid": "extension has errors", "not-built": "not built"}


def show(description, out):
    kind = f"variant of {description['agent']}" if description["variant"] else "extension of the agent's image"
    out(f"{description['target']} ({kind}) -> {description['image']}")
    for key in KINDS:
        out(f"  {key:9} {' '.join(description[key]) or '-'}")
    files = [f"{e['source']} -> {e['target']} ({e['mode']})" for e in description["files"]]
    out(f"  {'files':9} {', '.join(files) or '-'}")
    lines = [line for line in description["fragment"].splitlines() if line.strip()]
    out(f"  {'fragment':9} {len(lines)} line(s) in {Path(description['path']) / FRAGMENT}" if lines else
        f"  {'fragment':9} -")
    for item in description["findings"]:
        out(f"  {format_finding(item)}")


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def main(argv, out=print, run=subprocess.run, stdin=None):
    parser = argparse.ArgumentParser(prog="sandbox-ai images", description=__doc__.split("\n\n")[0],
                                     formatter_class=help_style,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:]))
    parser.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    parser.add_argument("-pu", "--podman-url", help="the URL of the Podman API socket to use instead of the default")
    sub = parser.add_subparsers(dest="command", metavar="{packages,extend,check,new,remove,history,recipe,pull,"
                                                        "build-image,list,os,search,derive}")
    listing = sub.add_parser("packages", help="what an image holds")
    listing.add_argument("agent", metavar="AGENT|VARIANT")
    listing.add_argument("-j", "--json", action="store_true", dest="json_sub", help="print the result as JSON (for programs)")
    ext = sub.add_parser("extend", help="change the extension of an agent's image or of a variant")
    ext.add_argument("target", metavar="AGENT|VARIANT")
    for kind, what in (("apt", "package of the image's distribution (Debian 12: apt-get; --add-os and --add-apt are "
                               "the same)"), ("npm", "global npm package"), ("pip", "Python tool (uv tool)")):
        aliases = ["--add-os"] if kind == "apt" else []
        ext.add_argument(*aliases, f"--add-{kind}", dest=f"add_{kind}", action="append", default=[], metavar="PKG",
                         help=f"add a {what}")
        ext.add_argument(*[a.replace("add", "remove") for a in aliases], f"--remove-{kind}", dest=f"remove_{kind}",
                         action="append", default=[], metavar="PKG", help=f"remove a {what}")
    ext.add_argument("--add-file", action="append", default=[], metavar="SRC:TARGET[:MODE]",
                     help="copy the host file SRC into the extension, installed at TARGET (mode 0644 by default)")
    ext.add_argument("--remove-file", action="append", default=[], metavar="TARGET", help="remove the file installed at TARGET from the extension (repeatable)")
    ext.add_argument("--fragment", metavar="FILE|-", help="replace the Containerfile fragment (- reads standard input)")
    ext.add_argument("--clear-fragment", action="store_true", help="remove the Containerfile fragment")
    ext.add_argument("--base", metavar="IMAGE", help="a variant: build it on your image IMAGE (sandbox-ai images list) "
                                                     "instead of the agent's own base; unchecked")
    ext.add_argument("--no-base", action="store_true", help="a variant: back on the agent's own base")
    ext.add_argument("--show", action="store_true", help="show the extension (the default without changes)")
    ext.add_argument("-j", "--json", action="store_true", dest="json_sub", help="print the result as JSON (for programs)")
    checking = sub.add_parser("check", help="validate an extension and show its final Containerfile")
    checking.add_argument("target", metavar="AGENT|VARIANT")
    checking.add_argument("--fragment", metavar="FILE|-", help="check this fragment instead of the saved one")
    checking.add_argument("-j", "--json", action="store_true", dest="json_sub", help="print the result as JSON (for programs)")
    cooked = sub.add_parser("recipe", help="a variant with a ready extension (images recipe --help lists them)",
                            description="A variant with a ready extension: " + "; ".join(
                                f"{key}: {value['description']}" for key, value in RECIPES.items()))
    cooked.add_argument("name", choices=sorted(RECIPES))
    cooked.add_argument("--from", dest="agent", required=True, choices=AGENTS, help="the agent whose extension it starts from")
    cooked.add_argument("--as", dest="variant", metavar="VARIANT", help="the variant's name (default: the recipe's)")
    new = sub.add_parser("new", help="a new variant: localhost/AGENT-sandbox-NAME")
    new.add_argument("name")
    new.add_argument("--from", dest="agent", required=True, choices=AGENTS, help="the agent whose extension it starts from")
    new.add_argument("--empty", action="store_true", help="start empty instead of from the agent's extension")
    new.add_argument("--base", metavar="IMAGE", help="build it on your image IMAGE (sandbox-ai images pull|build-image; "
                                                     "Debian 12) instead of the agent's own base; unchecked")
    new.add_argument("-j", "--json", action="store_true", dest="json_sub", help="print the result as JSON (for programs)")
    removing = sub.add_parser("remove", help="drop an extension or a variant, or forget one of your images",
                              description="Drop an extension; for a variant also its image; or forget one of your "
                                          "images (and remove it from Podman).")
    removing.add_argument("target", metavar="AGENT|VARIANT|IMAGE")
    removing.add_argument("--keep-image", action="store_true", help="forget it but leave its image in Podman")
    past = sub.add_parser("history", help="changes and builds of the extensions")
    past.add_argument("target", nargs="?", metavar="AGENT|VARIANT")
    past.add_argument("-j", "--json", action="store_true", dest="json_sub", help="print the result as JSON (for programs)")
    pulling = sub.add_parser("pull", help="pull an image from a registry, pinned by digest (unchecked)",
                             description="Pull an image from a registry once and record it, pinned by digest "
                                         "(unchecked); for --service and a variant's --base.")
    pulling.add_argument("reference", help="e.g. docker.io/library/redis:7-alpine, quay.io/org/app@sha256:...")
    pulling.add_argument("--as", dest="name", metavar="NAME", help="its name (default: the repository's last part)")
    pulling.add_argument("--replace", action="store_true", help="pin an image of that name anew")
    pulling.add_argument("-j", "--json", action="store_true", dest="json_sub", help="print the result as JSON (for programs)")
    owned = sub.add_parser("build-image", help="build your own Containerfile as one of your images (unchecked)",
                           description="Build your own Containerfile as one of your images, every FROM pulled once "
                                       "and pinned by digest (unchecked).")
    owned.add_argument("name")
    source = owned.add_mutually_exclusive_group(required=True)
    source.add_argument("-f", "--file", metavar="CONTAINERFILE", help="the Containerfile to build")
    source.add_argument("--builtin", metavar="TEMPLATE",
                        help="one of sandbox-ai's own Containerfiles (sandbox/service-images) instead, for the "
                             "showcase presets' services: showcase-web (nginx 1.28.0: a page of its own, /app/ to "
                             "localhost:8000)")
    owned.add_argument("--context", metavar="DIR", help="the build context (default: the Containerfile's folder)")
    owned.add_argument("--replace", action="store_true", help="build an image of that name anew")
    owned.add_argument("--trust-agent-written", action="store_true",
                       help="build a Containerfile or context that lies where a sandbox can write (you reviewed it)")
    owned.add_argument("--build-network", action="store_true",
                       help="give its RUN steps the network: this host's own, outside the sandbox's firewall, VPN and "
                            "Tor (default: none)")
    owned.add_argument("-j", "--json", action="store_true", dest="json_sub", help="print the result as JSON (for programs)")
    mine = sub.add_parser("list", help="your images from registries and Containerfiles, each marked unchecked")
    mine.add_argument("-j", "--json", action="store_true", dest="json_sub", help="print the result as JSON (for programs)")
    own = sub.add_parser("templates", help="the Containerfiles for services that come with sandbox-ai (build-image "
                                           "--builtin)",
                         description="The Containerfiles for services that come with sandbox-ai "
                                     "(sandbox/service-images), each with what it is and the images of yours built "
                                     "from it (images build-image NAME --builtin TEMPLATE; the app's start builds one "
                                     "when it needs it).")
    own.add_argument("-j", "--json", action="store_true", dest="json_sub", help="print the result as JSON (for programs)")
    system = sub.add_parser("os", help="the distribution of an image and its package manager",
                            description="The distribution of an image and the package manager its OS packages "
                                        "install with.")
    system.add_argument("target", metavar="AGENT|VARIANT|IMAGE")
    system.add_argument("-j", "--json", action="store_true", dest="json_sub", help="print the result as JSON (for programs)")
    finding = sub.add_parser("search", help="search the OS packages of an image's distribution",
                             description="Search the OS packages of an image's distribution (its package index, "
                                         "read once in a throwaway container of it and cached).")
    finding.add_argument("target", metavar="AGENT|VARIANT|IMAGE")
    finding.add_argument("query", nargs="?", default="")
    finding.add_argument("--refresh", action="store_true", help="read the package index anew")
    finding.add_argument("--limit", type=int, default=50, help="show at most this many packages (default 50)")
    finding.add_argument("-j", "--json", action="store_true", dest="json_sub", help="print the result as JSON (for programs)")
    deriving = sub.add_parser("derive", help="one of your images with OS packages added, as another (unchecked)",
                              description="One of your images with OS packages added by its own distribution's "
                                          "manager (apk, dnf, pacman, zypper, apt-get), recorded as another "
                                          "(unchecked).")
    deriving.add_argument("name")
    deriving.add_argument("--from", dest="source", required=True, metavar="IMAGE", help="your image to start from")
    deriving.add_argument("--add-os", "--add-apt", dest="add_os", action="append", default=[], metavar="PKG", help="add an OS package, installed by the image's own package manager (repeatable)")
    deriving.add_argument("--add-npm", action="append", default=[], help=argparse.SUPPRESS)
    deriving.add_argument("--add-pip", action="append", default=[], help=argparse.SUPPRESS)
    deriving.add_argument("--replace", action="store_true", help="build an image of that name anew (NAME may be "
                                                                 "IMAGE itself)")
    deriving.add_argument("-j", "--json", action="store_true", dest="json_sub", help="print the result as JSON (for programs)")
    # for sandbox-ai build: a variant's base image of yours, made ready
    prep = sub.add_parser("prep", help=argparse.SUPPRESS)
    prep.add_argument("target")
    plan = sub.add_parser("build-plan", help=argparse.SUPPRESS)  # for sandbox-ai build
    plan.add_argument("target")
    layer = sub.add_parser("layer", help=argparse.SUPPRESS)  # for sandbox-ai build: the extension layers after the base
    layer.add_argument("target")
    layer.add_argument("--variants", action="store_true", help="build the layers of the variants, not of the agents")
    args = parser.parse_args(argv)
    as_json = args.json or getattr(args, "json_sub", False)
    try:
        if args.command == "packages":
            rows = packages(args.agent, args.podman_url, run)
            if as_json:
                out(json.dumps([{"kind": k, "name": n, "version": v} for k, n, v in rows], indent=2))
            else:
                for kind, name, version in rows:
                    out(f"{kind:5} {name:40} {version}")
            return 0
        if args.command == "extend":
            if args.fragment is not None and args.clear_fragment:
                raise ValueError("--fragment and --clear-fragment exclude each other")
            if args.base and args.no_base:
                raise ValueError("--base and --no-base exclude each other")
            base = "" if args.no_base else args.base
            fragment = "" if args.clear_fragment else None if args.fragment is None else \
                read_fragment(args.fragment, stdin)
            add = {kind: getattr(args, f"add_{kind}") for kind in KINDS}
            drop = {kind: getattr(args, f"remove_{kind}") for kind in KINDS}
            changing = any(add.values()) or any(drop.values()) or args.add_file or args.remove_file or \
                fragment is not None or base is not None
            if changing:
                description, changes = extend(args.target, add, drop, args.add_file, args.remove_file, fragment,
                                              base)
            else:
                description, changes = describe(args.target, strict=True), []
            if as_json:
                out(json.dumps({**description, "changes": changes}, indent=2))
            else:
                for change in changes:
                    out(f"changed: {change}")
                if args.show or not changing:
                    show(description, out)
                else:
                    for item in description["findings"]:
                        out(format_finding(item))
                if changes:
                    out(f"build it with: {build_command(args.target) if args.target not in AGENTS else f'sandbox-ai build {args.target}'}")
            return 0
        if args.command == "check":
            agent = agent_of(args.target)
            document, fragment, files = load(args.target)
            if args.fragment is not None:
                fragment = read_fragment(args.fragment, stdin)
            findings = check(document, fragment, files)
            text = containerfile(args.target, document, fragment)
            if as_json:
                out(json.dumps({"target": args.target, "agent": agent, "findings": findings, "containerfile": text,
                                "hash": extension_hash(text, files)}, indent=2))
            else:
                for item in findings:
                    out(format_finding(item))
                out(f"{len(errors(findings))} error(s), {len(findings) - len(errors(findings))} warning(s)")
                out(text.rstrip("\n"))
            return 1 if errors(findings) else 0
        if args.command == "recipe":
            description, changes = recipe(args.name, args.agent, args.variant)
            variant = args.variant or args.name
            out(f"variant {variant} of {args.agent} with the recipe {args.name}: {', '.join(changes) or 'no change'}")
            out(f"build it: {build_command(variant)}; start with: sandbox-ai {args.agent} --image-variant {variant}")
            return 0
        if args.command == "new":
            description = new_variant(args.name, args.agent, args.empty, args.base)
            if as_json:
                out(json.dumps(description, indent=2))
            else:
                out(f"variant {args.name} of {args.agent}: {description['image']}")
                if args.base:
                    out(f"warning: {base_summary(args.name)['warning']}")
                out(f"change it with: sandbox-ai images extend {args.name} ...; build it with: "
                    f"{build_command(args.name)}; start it with: sandbox-ai {args.agent} --image-variant {args.name}")
            return 0
        if args.command == "remove":
            if args.target not in AGENTS and not any(name == args.target for name, _ in variants()) and \
                    args.target in services_module().read_registry():
                if args.podman_url:
                    raise ValueError("your images are recorded on this machine: remove one without --podman-url")
                for note in services_module().remove(args.target, args.keep_image, podman(), run):
                    out(note)
                out(f"forgot your image {args.target}")
                return 0
            for note in remove(args.target, args.keep_image, args.podman_url, run):
                out(note)
            out(f"removed the extension {args.target}")
            return 0
        if args.command == "templates":
            return services_module().main(["templates", *(["--json"] if as_json else [])], out=out, run=run)
        if args.command in ("pull", "build-image", "list"):
            if args.podman_url:
                raise ValueError(f"images {args.command}: your images are recorded on this machine, for its own Podman "
                                 "(on another one: sandbox-ai hosts run HOST -- images ...)")
            if args.command == "pull":
                return services_module().main(["pull", args.reference, *(["--as", args.name] if args.name else []),
                                               *(["--replace"] if args.replace else []),
                                               *(["--json"] if as_json else [])], out=out, run=run)
            if args.command == "build-image":
                return services_module().main(["build-image", args.name,
                                               *(["--builtin", args.builtin] if args.builtin else ["--file", args.file]),
                                               *(["--context", args.context] if args.context else []),
                                               *(["--replace"] if args.replace else []),
                                               *(["--trust-agent-written"] if args.trust_agent_written else []),
                                               *(["--build-network"] if args.build_network else []),
                                               *(["--json"] if as_json else [])], out=out, run=run)
            return services_module().main(["list", *(["--json"] if as_json else [])], out=out, run=run)
        if args.command == "prep":
            out(build_prep(args.target, args.podman_url, run))
            return 0
        if args.command == "os":
            found = distribution(args.target)
            if as_json:
                out(json.dumps(found, indent=2))
            else:
                out(f"{args.target}: {found['system']}; " + (
                    f"OS packages install with {found['manager']} ({found['label']})" if found["family"] else
                    "a distribution sandbox-ai has no package manager for"))
            return 0
        if args.command == "search":
            import signal
            # Cancelled (the app's Cancel, Ctrl+C): the throwaway container is removed on the way out (fetch_index).
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
            found = package_search(args.target, args.query, args.refresh, args.limit, args.podman_url, run)
            if as_json:
                out(json.dumps(found, indent=2))
                return 0
            out(f"{found['system']} ({found['manager']}): {len(found['results'])} of {found['count']} packages")
            if found.get("stale"):
                out(f"note: {found['stale']}")
            for row in found["results"]:
                out(f"{row['name']:36} {row['version']:24} {row['summary']}")
            return 0
        if args.command == "derive":
            if args.podman_url:
                raise ValueError("images derive: your images are recorded on this machine, for its own Podman")
            name, entry = derive(args.name, args.source, args.add_os, args.replace, run=None if run is subprocess.run
                                 else run, out=(lambda text: print(text, file=sys.stderr)) if as_json else out,
                                 npm=args.add_npm, pip=args.add_pip)
            if as_json:
                out(json.dumps({"name": name, **entry}, indent=2))
            else:
                out(f"sandbox-ai images: {name} is one of your images now (UNCHECKED); start it as a service: "
                    f"sandbox-ai claude --service {name}")
            return 0
        if args.command == "history":
            entries = history(args.target)
            if as_json:
                out(json.dumps(entries, indent=2))
            for entry in [] if as_json else entries:
                extra = " ".join(entry.get("changes", [])) or (entry.get("hash") or "")[:12]
                out(f"{entry.get('time', '?'):20} {entry.get('action', '?'):12} {entry.get('target', '?'):16} {extra}")
            return 0
        if args.command == "build-plan":
            agent, kind, count = build_plan(args.target)
            out(f"{agent} {kind} {count}")
            return 0
        if args.command == "layer":
            return build_layers(args.target, args.variants, args.podman_url, run, out)
        found = images(args.podman_url, run)
        if as_json:
            out(json.dumps(found, indent=2))
            return 0
        for image in found:
            label = image["target"] if not image["variant"] else f"{image['target']} ({image['agent']})"
            if not image["present"]:
                out(f"{label:18} not built ({build_command(image['target']) if image['variant'] else 'sandbox-ai build ' + image['target']})"
                    + ("; the extension has errors" if image["status"] == "invalid" else ""))
            else:
                size = f"{image['size'] / 1e9:.1f} GB"
                extension = STATUS_TEXT[image["status"]] if image["extended"] else "no extension"
                out(f"{label:18} {image['image']:44} {image['agent_version'] or '?':12} {image['created']}  "
                    f"{size:>7}  {extension}"
                    + (f"  on your image {image['base_image']} (UNCHECKED)" if image.get("base_image") else ""))
        try:
            mine = services_module().read_registry_parts()[0]  # (a damaged record hides none of the others)
        except ValueError:
            mine = {}
        if mine:
            out(f"your images: {', '.join(sorted(mine))} (UNCHECKED; sandbox-ai images list)")
        return 0
    except ExtensionError as exc:
        print(f"sandbox-ai images: {exc}", file=sys.stderr)
        for item in exc.findings:
            print(f"  {format_finding(item)}", file=sys.stderr)
        if as_json:
            out(json.dumps({"error": str(exc), "findings": exc.findings}, indent=2))
        return 1
    except FileNotFoundError as exc:
        missing = exc.filename or str(exc)
        print(f"sandbox-ai images: {missing} is not installed here" + (
            "; sandbox-ai doctor says what to install" if "podman" in str(missing) else ""), file=sys.stderr)
        return 1
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"sandbox-ai images: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
