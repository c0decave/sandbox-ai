#!/usr/bin/env python3
"""sandbox-ai images: the agent images on this host, what is in them, and your extensions of them.

  sandbox-ai images [--json] [--podman-url URL]          images and variants: agent version, created, size,
                                                         whether each is current with its extension
  sandbox-ai images packages AGENT|VARIANT [--json]      Debian packages, global npm packages and tools in one
  sandbox-ai images extend AGENT|VARIANT [--add-apt PKG] [--remove-apt PKG] [--add-npm PKG] [--remove-npm PKG]
                     [--add-pip PKG] [--remove-pip PKG] [--add-file SRC:TARGET[:MODE]] [--remove-file TARGET]
                     [--fragment FILE|-] [--clear-fragment] [--show] [--json]
  sandbox-ai images check AGENT|VARIANT [--fragment FILE|-] [--json]
                                                         validate the extension, show the final Containerfile
  sandbox-ai images recipe NAME --from AGENT [--as V]  a variant with a ready extension (media: ffmpeg,
                                                         PulseAudio, espeak-ng, Piper ... for --audio virtual and
                                                         videos; tor: torsocks, torbrowser-launcher; dev: Podman
                                                         and tools for developing sandbox-ai, with
                                                         --nested-containers; hyperframes, playwright: the CLIs
                                                         of the built-in kits' skills, pinned by lockfiles)
  sandbox-ai images new NAME --from AGENT [--empty]      a variant: localhost/AGENT-sandbox-NAME, started with
                                                         --image-variant NAME
  sandbox-ai images remove AGENT|VARIANT [--keep-image]  drop an extension (and a variant's image)
  sandbox-ai images history [AGENT|VARIANT] [--json]     changes and builds of the extensions
  sandbox-ai images new NAME --from AGENT --base IMAGE   a variant built on one of your images (Debian 12) instead
                                                         of the agent's own base: the agent's layers on top of it
  sandbox-ai images pull REFERENCE [--as NAME] [--replace]  one of your images from a registry (Docker Hub, quay.io,
                                                         ghcr.io, ...): pulled once, now, and pinned by its digest
  sandbox-ai images build-image NAME --file CONTAINERFILE [--context DIR] [--replace] [--trust-agent-written]
                             [--build-network]
                                                         one of your images from your own Containerfile: each FROM
                                                         pulled once and pinned by digest, the result by its ID;
                                                         its RUN steps without network unless --build-network (this
                                                         host's own, outside the firewall, VPN and Tor); refused
                                                         where a sandbox can write unless --trust-agent-written
  sandbox-ai images list [--json]                        your images, each marked UNCHECKED (nobody reviewed them)
  sandbox-ai images remove IMAGE [--keep-image]          forget one of your images

Your images run as services beside the agent (sandbox-ai claude --service NAME[:port=PORT,...]: behind the
sandbox's firewall and proxy, no capabilities, a read-only root, limits of their own) or as the base of a
variant. Starts never pull: an image is pinned when you add it, and its age does not matter.

An extension adds Debian packages, global npm packages, Python tools (uv tool), files and an optional
Containerfile fragment on top of the agent's base image. Extensions live in your configuration directory
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
AGENTS = ("claude", "codex", "opencode")
LABEL = "sandbox-ai.agent-version"
HASH_LABEL = "sandbox-ai.extension-hash"
BASE_LABEL = "sandbox-ai.base-id"
EXTENSION_LABEL = "sandbox-ai.extension"
AGENT_PACKAGES = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex", "opencode": "opencode-ai"}
NAME = r"[a-z0-9][a-z0-9_.-]{0,29}"
RESERVED = {*AGENTS, "all", "base", "latest", "sandbox", "team", "vpn"}  # team, vpn: sandbox-ai build targets
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


def image_name(target, agent):
    return f"localhost/{agent}-sandbox:latest" if target == agent else f"localhost/{agent}-sandbox-{target}:latest"


def base_image(agent):
    return f"localhost/{agent}-sandbox:base"


def base_of(target, document):
    """The image TARGET's extension layer is built on: the agent's base, or for a variant on a base image of yours
    (sandbox-ai images new NAME --base IMAGE) its own: the agent's layers built on that image."""
    agent = document["agent"]
    return f"localhost/{agent}-sandbox-{target}:base" if document.get("base") and target != agent else base_image(agent)


def prep_image(target, agent):
    """Your base image made ready for the agent's layers (prep_containerfile), tagged for sandbox-ai build."""
    return f"localhost/{agent}-sandbox-{target}:prep"


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


def variants():
    """[(name, agent)] of the variants in the configuration directory, sorted by name."""
    found = []
    root = config_root()
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return []
    for name in entries:
        if not valid_name(name):
            continue
        try:
            document = json.loads(read_limited(root / name / DOCUMENT, 1 << 20))
        except (ValueError, OSError):
            continue
        if isinstance(document, dict) and document.get("agent") in AGENTS:
            found.append((name, document["agent"]))
    return found


def targets():
    """[(name, agent)]: the agents' own extensions first, then the variants."""
    return [(agent, agent) for agent in AGENTS] + variants()


def agent_of(target):
    """The agent of TARGET (an agent or an existing variant), else ValueError."""
    if target in AGENTS:
        return target
    if not valid_name(target):
        raise ValueError(f"{target!r} is neither an agent ({', '.join(AGENTS)}) nor a variant name "
                         "(lower-case letters, digits, '.', '_' and '-', up to 30 characters)")
    for name, agent in variants():
        if name == target:
            return agent
    raise ValueError(f"no variant {target}; create it with: sandbox-ai images new {target} --from AGENT")


BUILD_TARGETS = ("codex", "claude", "opencode", "all", "team", "vpn", "tor")  # what `sandbox-ai build NAME` means


def build_command(name):
    """How the variant NAME is built: sandbox-ai build NAME, or with --variant when NAME is a build target too (the
    recipe tor's variant: `build tor` builds the Tor VPN's image)."""
    return f"sandbox-ai build --variant {name}" if name in BUILD_TARGETS else f"sandbox-ai build {name}"


def variant_image(agent, name):
    """The image of variant NAME of AGENT (for the launcher's --image-variant), else ValueError."""
    if not valid_name(name):
        raise ValueError(f"--image-variant {name!r}: not a variant name")
    try:
        owner = agent_of(name)
    except ValueError:
        if name in RECIPES:  # (images new would make an empty one: the recipe brings what the name promises)
            raise ValueError(f"--image-variant {name}: no such variant; make it from its recipe and build it: "
                             f"sandbox-ai images recipe {name} --from {agent} && {build_command(name)}") from None
        raise ValueError(f"--image-variant {name}: no such variant; create it with: sandbox-ai images new {name} "
                         f"--from {agent}") from None
    if owner != agent:
        raise ValueError(f"--image-variant {name} is a variant of {owner}, not of {agent}")
    return image_name(name, agent)


def load(target, strict=True):
    """(document, fragment text, {file name: bytes}) of TARGET's extension (empty for an agent without one).
    STRICT checks the location first (always before writing or building)."""
    agent = agent_of(target)
    directory = config_root() / target
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
        except ValueError:
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
    lines = [f"# Extension {target} of {agent}: generated by sandbox-ai images (change it with "
             f"sandbox-ai images extend {target})",
             f"FROM {base_of(target, document)}", "USER root"]
    if document.get("apt"):
        lines += ["RUN apt-get update && apt-get install -y --no-install-recommends \\",
                  "    " + " ".join(q(p) for p in document["apt"]) + " \\", "    && rm -rf /var/lib/apt/lists/*"]
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
    return {"target": target, "agent": agent, "variant": target != agent, "image": image_name(target, agent),
            "base": base_of(target, document), "base_image": document.get("base"), "path": str(config_root() / target),
            **{kind: list(document.get(kind, [])) for kind in KINDS}, "files": list(document.get("files", [])),
            "available": sorted(files), "fragment": fragment, "extended": has_extension(document, fragment, files),
            "findings": findings, "containerfile": text, "hash": extension_hash(text, files)}


# -- changes --

def write_private(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)


def remember(action, target, **details):
    """One line in the history of the extensions (changes and builds)."""
    path = history_file()
    entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "action": action, "target": target,
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
        except ValueError:
            continue
        if isinstance(entry, dict) and (target is None or entry.get("target") == target):
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
    directory = config_root() / target
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
        result = run(podman(podman_url) + ["build", *network, "--pull=never", "--tag", prep_image(target, agent),
                                           "--label", f"sandbox-ai.base-image={name}",
                                           "--label", f"sandbox-ai.base-image-id={entry['id']}",
                                           "--file", str(Path(context) / "Containerfile"), context],
                     stdin=subprocess.DEVNULL, stdout=sys.stderr)  # standard output: only the image's name
    if result.returncode != 0:
        remember("prep-failed", target, agent=agent, base=name)
        raise ValueError(f"preparing your image {name} for {agent} failed (exit status {result.returncode}); its "
                         "output above says why")
    remember("prep", target, agent=agent, base=name, image=entry["image"])
    return prep_image(target, agent)


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


# Ready extensions for a purpose: `sandbox-ai images recipe NAME --from AGENT` makes the variant NAME with them.
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
    # tor itself runs in the VPN container (sandbox-ai vpn add NAME --tor); the agent needs only its clients.
    # Tor Browser's libraries (GTK, X11, ALSA, dbus-glib) are in the base image's GUI layer (Playwright's Firefox).
    "tor": {"description": "for a sandbox behind a Tor VPN profile: torsocks (socks5h://127.0.0.1:9050 with vpn add "
                           "--socks) and torbrowser-launcher from Debian's contrib section, which downloads Tor Browser "
                           "and checks its signature at its first start (run it with --gui and TOR_SKIP_LAUNCH=1)",
            "apt": ["torsocks"],
            "fragment": "# torbrowser-launcher is in Debian's contrib section: its archive, signed by Debian's own key.\n"
                        "RUN . /etc/os-release \\\n"
                        "    && printf 'Types: deb\\nURIs: http://deb.debian.org/debian\\nSuites: %s %s-updates\\n"
                        "Components: contrib\\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\\n' \\\n"
                        "       \"$VERSION_CODENAME\" \"$VERSION_CODENAME\" > /etc/apt/sources.list.d/contrib.sources \\\n"
                        "    && apt-get update && apt-get install -y --no-install-recommends torbrowser-launcher \\\n"
                        "    && rm -rf /var/lib/apt/lists/*\n"},
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
}
RECIPE_FILES = HERE / "recipes"  # the files the recipes copy into the image (part of sandbox-ai, read-only)


def recipe(name, agent, variant=None):
    """The variant VARIANT (default NAME) of AGENT with the recipe NAME's packages; build it with sandbox-ai build."""
    if name not in RECIPES:
        raise ValueError(f"no recipe {name!r}; recipes: {', '.join(sorted(RECIPES))}")
    variant = variant or name
    new_variant(variant, agent)
    wanted = RECIPES[name]
    fragment = None
    if wanted.get("fragment"):  # after the agent's own fragment, which the variant copied
        own = load(variant)[1].rstrip("\n")
        fragment = (own + "\n" if own.strip() else "") + wanted["fragment"]
    own_files = [(target, mode, read_limited(RECIPE_FILES / source, FILE_LIMIT))
                 for source, target, mode in wanted.get("files", [])]
    return extend(variant, add={kind: list(wanted.get(kind, [])) for kind in KINDS if wanted.get(kind)},
                  fragment=fragment, own_files=own_files)


def new_variant(name, agent, empty=False, base=None):
    """The variant NAME of AGENT: its extension copied from the agent's (unless EMPTY); BASE: one of your images
    (sandbox-ai images pull|build-image) it is built on instead of the agent's own base."""
    if not valid_name(name):
        raise ValueError(f"{name!r}: a variant name is lower-case letters, digits, '.', '_' and '-' (up to 30 "
                         f"characters), and not one of {', '.join(sorted(RESERVED))}")
    if agent not in AGENTS:
        raise ValueError(f"--from expects one of {', '.join(AGENTS)}")
    check_location(config_root(), "The image extensions in")
    directory = config_root() / name
    if directory.exists() or directory.is_symlink():
        raise ValueError(f"{name} exists already ({directory})")
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
    return describe(name)


def remove(target, keep_image=False, podman_url=None, run=subprocess.run):
    """Drop TARGET's extension; for a variant also its image (unless KEEP_IMAGE). Returns notes."""
    agent = agent_of(target)
    check_location(config_root(), "The image extensions in")
    directory = config_root() / target
    notes = []
    try:
        based = target != agent and bool(load(target, strict=False)[0].get("base"))
    except ValueError:
        based = False
    if directory.is_symlink():
        os.unlink(directory)
    elif directory.exists():
        shutil.rmtree(directory)
    if target == agent:
        notes.append(f"the next sandbox-ai build {agent} leaves the extension out")
    elif not keep_image:
        # A variant on a base of yours has its own base layers (and their preparation) too.
        for image in [image_name(target, agent)] + ([f"localhost/{agent}-sandbox-{target}:base",
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
    result = run(podman(podman_url) + ["image", "inspect", "--format", "json", name], capture_output=True, text=True,
                 timeout=60, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)[0]
        return data if isinstance(data, dict) else None
    except (ValueError, IndexError, KeyError, TypeError):
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
    if agent not in AGENTS and not (valid_name(agent) and any(name == agent for name, _ in variants())):
        raise ValueError(f"agent must be one of {', '.join(AGENTS)} or a variant")
    image = image_name(agent, agent_of(agent))
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
                                     str(context / "Containerfile"), "--label", f"{EXTENSION_LABEL}={target}",
                                     "--label", f"{HASH_LABEL}={digest}", "--label", f"{BASE_LABEL}={base_id}",
                                     str(context)]
        if run is subprocess.run:  # its steps said as they go, bounded (progress.py; SANDBOX_AI_PODMAN_OUTPUT=full)
            argv = [sys.executable, "-I", str(HERE / "progress.py"), "podman", *argv]
        result = run(argv, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        remember("build-failed", target, agent=agent, hash=digest)
        raise ValueError(f"podman build of the extension {target} failed (exit status {result.returncode})")
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
    jobs = [target] if target != agent else [agent] + ([n for n, a in variants() if a == agent] if with_variants else [])
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
            print(f"sandbox-ai images: {exc}", file=sys.stderr)
            for item in errors(exc.findings):
                print(f"  {format_finding(item)}", file=sys.stderr)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            failed += 1
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


def main(argv, out=print, run=subprocess.run, stdin=None):
    parser = argparse.ArgumentParser(prog="sandbox-ai images", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:]))
    parser.add_argument("-j", "--json", action="store_true")
    parser.add_argument("-pu", "--podman-url")
    sub = parser.add_subparsers(dest="command", metavar="{packages,extend,check,new,remove,history,recipe,pull,"
                                                        "build-image,list}")
    listing = sub.add_parser("packages", help="what an image holds")
    listing.add_argument("agent", metavar="AGENT|VARIANT")
    listing.add_argument("-j", "--json", action="store_true", dest="json_sub")
    ext = sub.add_parser("extend", help="change the extension of an agent's image or of a variant")
    ext.add_argument("target", metavar="AGENT|VARIANT")
    for kind, what in (("apt", "Debian package"), ("npm", "global npm package"), ("pip", "Python tool (uv tool)")):
        ext.add_argument(f"--add-{kind}", action="append", default=[], metavar="PKG", help=f"add a {what}")
        ext.add_argument(f"--remove-{kind}", action="append", default=[], metavar="PKG", help=f"remove a {what}")
    ext.add_argument("--add-file", action="append", default=[], metavar="SRC:TARGET[:MODE]",
                     help="copy the host file SRC into the extension, installed at TARGET (mode 0644 by default)")
    ext.add_argument("--remove-file", action="append", default=[], metavar="TARGET")
    ext.add_argument("--fragment", metavar="FILE|-", help="replace the Containerfile fragment (- reads standard input)")
    ext.add_argument("--clear-fragment", action="store_true")
    ext.add_argument("--base", metavar="IMAGE", help="a variant: build it on your image IMAGE (sandbox-ai images list) "
                                                     "instead of the agent's own base; unchecked")
    ext.add_argument("--no-base", action="store_true", help="a variant: back on the agent's own base")
    ext.add_argument("--show", action="store_true", help="show the extension (the default without changes)")
    ext.add_argument("-j", "--json", action="store_true", dest="json_sub")
    checking = sub.add_parser("check", help="validate an extension and show its final Containerfile")
    checking.add_argument("target", metavar="AGENT|VARIANT")
    checking.add_argument("--fragment", metavar="FILE|-", help="check this fragment instead of the saved one")
    checking.add_argument("-j", "--json", action="store_true", dest="json_sub")
    cooked = sub.add_parser("recipe", help="a variant with a ready extension: " + "; ".join(
        f"{key}: {value['description']}" for key, value in RECIPES.items()))
    cooked.add_argument("name", choices=sorted(RECIPES))
    cooked.add_argument("--from", dest="agent", required=True, choices=AGENTS)
    cooked.add_argument("--as", dest="variant", metavar="VARIANT", help="the variant's name (default: the recipe's)")
    new = sub.add_parser("new", help="a new variant: localhost/AGENT-sandbox-NAME")
    new.add_argument("name")
    new.add_argument("--from", dest="agent", required=True, choices=AGENTS)
    new.add_argument("--empty", action="store_true", help="start empty instead of from the agent's extension")
    new.add_argument("--base", metavar="IMAGE", help="build it on your image IMAGE (sandbox-ai images pull|build-image; "
                                                     "Debian 12) instead of the agent's own base; unchecked")
    new.add_argument("-j", "--json", action="store_true", dest="json_sub")
    removing = sub.add_parser("remove", help="drop an extension; for a variant also its image; or forget one of your "
                                             "images (and remove it from Podman)")
    removing.add_argument("target", metavar="AGENT|VARIANT|IMAGE")
    removing.add_argument("--keep-image", action="store_true")
    past = sub.add_parser("history", help="changes and builds of the extensions")
    past.add_argument("target", nargs="?", metavar="AGENT|VARIANT")
    past.add_argument("-j", "--json", action="store_true", dest="json_sub")
    pulling = sub.add_parser("pull", help="pull an image from a registry once and record it, pinned by digest "
                                          "(unchecked); for --service and a variant's --base")
    pulling.add_argument("reference", help="e.g. docker.io/library/redis:7-alpine, quay.io/org/app@sha256:...")
    pulling.add_argument("--as", dest="name", metavar="NAME", help="its name (default: the repository's last part)")
    pulling.add_argument("--replace", action="store_true", help="pin an image of that name anew")
    pulling.add_argument("-j", "--json", action="store_true", dest="json_sub")
    owned = sub.add_parser("build-image", help="build your own Containerfile as one of your images, every FROM "
                                               "pulled once and pinned by digest (unchecked)")
    owned.add_argument("name")
    owned.add_argument("-f", "--file", required=True, metavar="CONTAINERFILE")
    owned.add_argument("--context", metavar="DIR", help="the build context (default: the Containerfile's folder)")
    owned.add_argument("--replace", action="store_true", help="build an image of that name anew")
    owned.add_argument("--trust-agent-written", action="store_true",
                       help="build a Containerfile or context that lies where a sandbox can write (you reviewed it)")
    owned.add_argument("--build-network", action="store_true",
                       help="give its RUN steps the network: this host's own, outside the sandbox's firewall, VPN and "
                            "Tor (default: none)")
    owned.add_argument("-j", "--json", action="store_true", dest="json_sub")
    mine = sub.add_parser("list", help="your images from registries and Containerfiles, each marked unchecked")
    mine.add_argument("-j", "--json", action="store_true", dest="json_sub")
    prep = sub.add_parser("prep")  # for sandbox-ai build: a variant's base image of yours, made ready
    prep.add_argument("target")
    plan = sub.add_parser("build-plan")  # for sandbox-ai build
    plan.add_argument("target")
    layer = sub.add_parser("layer")  # for sandbox-ai build: the extension layers after the base
    layer.add_argument("target")
    layer.add_argument("--variants", action="store_true")
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
        if args.command in ("pull", "build-image", "list"):
            if args.podman_url:
                raise ValueError(f"images {args.command}: your images are recorded on this machine, for its own Podman "
                                 "(on another one: sandbox-ai hosts run HOST -- images ...)")
            if args.command == "pull":
                return services_module().main(["pull", args.reference, *(["--as", args.name] if args.name else []),
                                               *(["--replace"] if args.replace else []),
                                               *(["--json"] if as_json else [])], out=out, run=run)
            if args.command == "build-image":
                return services_module().main(["build-image", args.name, "--file", args.file,
                                               *(["--context", args.context] if args.context else []),
                                               *(["--replace"] if args.replace else []),
                                               *(["--trust-agent-written"] if args.trust_agent_written else []),
                                               *(["--build-network"] if args.build_network else []),
                                               *(["--json"] if as_json else [])], out=out, run=run)
            return services_module().main(["list", *(["--json"] if as_json else [])], out=out, run=run)
        if args.command == "prep":
            out(build_prep(args.target, args.podman_url, run))
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
            mine = services_module().read_registry()
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
