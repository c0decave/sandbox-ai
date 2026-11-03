#!/usr/bin/env python3
"""Privacy check: what a push would publish, checked before it is published.

Rules come from three places:

  built-in   credential shapes: private keys, cloud, Git and API tokens
  derived    what sandbox-ai knows is yours: the host HOME, your git e-mail address, the host name,
             private addresses from --allow-tcp, your own Git servers with their account names, and
             the tokens of --git-token-* and --git-credentials
  your own   global.rules.toml and PROJECT.rules.toml (or .json) in --privacy-rules DIR, by default
             ~/.config/sandbox-ai/privacy; PROJECT is the workspace's directory name. The format is
             the one of scrub_guard: [[pattern]] id, regex, severity ("block" or "warn"), reason;
             [[allow]] id, regex, reason and an optional path_glob. Keep the directory outside every
             workspace: it holds the very strings it forbids.

"What a push would publish" is: commits on local branches that no remote-tracking branch has (the
files they add or change, author, committer and message), staged changes, tracked files changed
in the work tree and untracked files git does not ignore.

  sandbox-ai privacy-check [PATH ...] [--rules-dir DIR] [--all] [--json]

Exit codes: 0 clean or warnings only, 1 blocking findings, 2 refused (unreadable rules, not a
repository). A rule set that cannot be read is a refusal, never a pass.

The launcher also runs the check after every run (see --privacy-check) and gives the agent
pre-commit and pre-push hooks with the built-in and derived rules; this file is their engine
(``privacy.py hook NAME``). Git runs on the host only with plumbing commands that start no
program from the repository's configuration (no fsmonitor, filters, textconv or hooks).
"""
import argparse
from dataclasses import dataclass, field
import errno
import fnmatch
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
from typing import Optional
import urllib.parse

HERE = Path(__file__).resolve().parent
BLOCK, WARN = "block", "warn"
EXIT_CLEAN, EXIT_FINDINGS, EXIT_REFUSED = 0, 1, 2

FILE_LIMIT = 1 << 20       # bytes per file or blob; larger ones are named as skipped
TOTAL_LIMIT = 256 << 20    # bytes read per repository
FILES_LIMIT = 50000        # work tree files per repository
COMMITS_LIMIT = 1000       # commits per check
RULES_LIMIT = 1 << 20      # bytes per rule file
GIT_TIMEOUT = 300
SHOWN = 20                 # findings printed after a run
TEXT_WIDTH = 160
ZERO = re.compile(r"0+")


class RulesUnavailable(ValueError):
    """No usable rule set, or no usable repository: always a refusal, never a pass."""


# --- rules -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Pattern:
    id: str
    regex: re.Pattern
    severity: str
    reason: str
    secret: bool = False  # the match is masked wherever a line is shown


@dataclass(frozen=True)
class Allow:
    id: str
    regex: re.Pattern
    reason: str
    path_glob: Optional[str] = None

    def covers(self, path, line):
        if self.path_glob and not fnmatch.fnmatch(path, self.path_glob):
            return False
        return bool(self.regex.search(line))


@dataclass
class Rules:
    patterns: list = field(default_factory=list)
    allows: list = field(default_factory=list)
    sources: list = field(default_factory=list)

    def add(self, other):
        self.patterns += other.patterns
        self.allows += other.allows
        self.sources += other.sources
        return self


# Credential shapes. The fixtures in the tests are assembled at run time, so that no scanner takes
# them for real keys; keep it that way.
BUILTIN = [
    ("private-key", r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----", BLOCK, "private key material"),
    ("aws-access-key-id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b", BLOCK, "AWS access key id"),
    ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{20,}", BLOCK, "GitHub token"),
    ("github-fine-grained-token", r"\bgithub_pat_[A-Za-z0-9_]{22,}", BLOCK, "GitHub fine-grained token"),
    ("gitlab-token", r"\bglpat-[A-Za-z0-9_-]{20,}", BLOCK, "GitLab token"),
    ("anthropic-api-key", r"\bsk-ant-[A-Za-z0-9_-]{20,}", BLOCK, "Anthropic API key"),
    ("openai-api-key", r"\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}", BLOCK, "OpenAI-style API key"),
    ("slack-token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}", BLOCK, "Slack token"),
    ("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])", BLOCK, "Google API key"),
    ("stripe-secret-key", r"\b[rs]k_live_[0-9A-Za-z]{20,}", BLOCK, "Stripe secret key"),
    ("npm-token", r"\bnpm_[A-Za-z0-9]{36}\b", BLOCK, "npm token"),
    ("pypi-token", r"\bpypi-AgE[A-Za-z0-9_-]{50,}", BLOCK, "PyPI token"),
    ("huggingface-token", r"\bhf_[A-Za-z0-9]{34,}\b", BLOCK, "Hugging Face token"),
    ("password-in-url", r"\b[a-z][a-z0-9+.-]*://[^\s/:@'\"<>]+:[^\s/@'\"<>]{6,}@[^\s/'\"<>]+", WARN,
     "user and password in a URL"),
]


def builtin_rules():
    rules = Rules(sources=["built-in"])
    for id_, regex, severity, reason in BUILTIN:
        rules.patterns.append(Pattern(id_, re.compile(regex), severity, reason, secret=True))
    return rules


def parse_rules(document, origin):
    """Rules from a parsed TOML or JSON document in scrub_guard's format; RulesUnavailable if unusable."""
    if not isinstance(document, dict):
        raise RulesUnavailable(f"{origin}: expected a table of rules")
    unknown = set(document) - {"meta", "pattern", "allow", "exclude"}
    if unknown:
        raise RulesUnavailable(f"{origin}: unknown section(s) {sorted(unknown)}; "
                               "expected pattern, allow, exclude and meta")
    rules = Rules(sources=[origin])
    for kind in ("pattern", "allow", "exclude"):
        entries = document.get(kind, [])
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            raise RulesUnavailable(f"{origin}: {kind} must be a list of tables ([[{kind}]])")
    for entry in document.get("pattern", []):
        _require(entry, ("id", "regex", "severity", "reason"), origin, "pattern")
        if entry["severity"] not in (BLOCK, WARN):
            raise RulesUnavailable(f"{origin}: pattern {entry['id']!r} has unknown severity {entry['severity']!r}")
        if not isinstance(entry.get("secret", False), bool):
            raise RulesUnavailable(f"{origin}: pattern {entry['id']!r}: secret must be true or false")
        rules.patterns.append(Pattern(str(entry["id"]), _compile(entry, origin, "pattern"), entry["severity"],
                                      str(entry["reason"]), entry.get("secret", False)))
    for entry in document.get("allow", []):
        _require(entry, ("id", "regex", "reason"), origin, "allow")
        glob = entry.get("path_glob")
        if glob is not None and not isinstance(glob, str):
            raise RulesUnavailable(f"{origin}: allow {entry['id']!r}: path_glob must be a string")
        rules.allows.append(Allow(str(entry["id"]), _compile(entry, origin, "allow"), str(entry["reason"]), glob))
    for entry in document.get("exclude", []):
        # Only the export of scrub_guard leaves these files out; a push publishes them all the same.
        _require(entry, ("id", "path_glob", "reason"), origin, "exclude")
    return rules


def _require(entry, keys, origin, kind):
    for key in keys:
        if key not in entry:
            raise RulesUnavailable(f"{origin}: {kind} is missing {key!r}: {entry.get('id', entry)!r}")
    if not isinstance(entry.get("regex", ""), str):
        raise RulesUnavailable(f"{origin}: {kind} {entry['id']!r}: regex must be a string")


def _compile(entry, origin, kind):
    try:
        return re.compile(entry["regex"])
    except re.error as exc:
        raise RulesUnavailable(f"{origin}: {kind} {entry['id']!r} has an uncompilable regex: {exc}") from None


def read_rule_file(path):
    """The rules in PATH (.toml or .json). Never follows a symlink; RulesUnavailable when unusable."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        reason = "it is a symlink" if exc.errno == errno.ELOOP else exc.strerror
        raise RulesUnavailable(f"{path}: cannot read: {reason}") from None
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RulesUnavailable(f"{path}: not a regular file")
        data = stream.read(RULES_LIMIT + 1)
    if len(data) > RULES_LIMIT:
        raise RulesUnavailable(f"{path}: larger than {RULES_LIMIT >> 20} MiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise RulesUnavailable(f"{path}: not UTF-8") from None
    if path.suffix == ".json":
        try:
            document = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise RulesUnavailable(f"{path}: invalid JSON: {exc}") from None
    else:
        toml = _toml_module()
        if toml is None:
            raise RulesUnavailable(f"{path}: reading TOML needs Python 3.11 or the tomli package; "
                                   f"or write the same rules as {path.with_suffix('.json').name}")
        try:
            document = toml.loads(text)
        except Exception as exc:  # noqa: BLE001 - tomllib and tomli raise their own error types
            raise RulesUnavailable(f"{path}: malformed TOML: {exc}") from None
    return parse_rules(document, str(path))


def _toml_module():
    for name in ("tomllib", "tomli"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def default_rules_dir():
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "sandbox-ai" / "privacy"


def user_rules(rules_dir, project):
    """Your own rules: RULES_DIR/global.rules.* and RULES_DIR/PROJECT.rules.*; both are optional."""
    rules = Rules()
    if rules_dir is None:
        return rules
    rules_dir = Path(rules_dir).expanduser()
    if not rules_dir.is_dir():
        raise RulesUnavailable(f"privacy rules directory {rules_dir} does not exist")
    stems = ["global"] + ([project] if project and project not in ("global", ".", "..") and "/" not in project else [])
    for stem in stems:
        found = [rules_dir / f"{stem}.rules.{suffix}" for suffix in ("toml", "json")
                 if os.path.lexists(rules_dir / f"{stem}.rules.{suffix}")]
        if len(found) > 1:
            raise RulesUnavailable(f"{rules_dir}: both {found[0].name} and {found[1].name}; keep one")
        if found:
            rules.add(read_rule_file(found[0]))
    return rules


# --- rules derived from what sandbox-ai knows ------------------------------------------------------

PUBLIC_GIT_HOSTS = {"github.com", "gitlab.com", "codeberg.org", "bitbucket.org", "gitea.com", "git.sr.ht",
                    "ssh.dev.azure.com", "dev.azure.com", "huggingface.co"}
PRIVATE_SUFFIXES = (".local", ".lan", ".internal", ".intranet", ".corp", ".home", ".localdomain", ".home.arpa",
                    ".test", ".localhost")
GENERIC_HOSTNAMES = {"localhost", "linux", "arch", "archlinux", "debian", "ubuntu", "fedora", "nixos", "manjaro",
                     "kali", "mint", "pop-os", "raspberrypi", "desktop", "laptop", "host", "server", "workstation",
                     "computer", "pc", "toolbox", "container", "sandbox"}
GENERIC_USERS = {"git", "oauth2", "oauth", "x-access-token", "x-token-auth", "token", "user", "admin", "root",
                 "api", "bot", "ci", "gitea", "forgejo", "gitlab", "github", "pat", "private-token"}


DOCUMENTATION = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
                                                  "2001:db8::/32")]


def host_kind(host):
    """"loopback", "private", "public" (a known Git host) or "custom" for a Git server's host name."""
    host = host.lower().strip("[]").rstrip(".")
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        return "loopback"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if address.is_loopback:
            return "loopback"
        return "private" if address.is_private or address.is_link_local else "custom"
    if host in PUBLIC_GIT_HOSTS:
        return "public"
    if "." not in host or host.endswith(PRIVATE_SUFFIXES):
        return "private"
    return "custom"


def _word(text, case=True):
    """TEXT as a whole word: not part of a longer name, path segment or number."""
    body = re.escape(text)
    if not case:
        body = f"(?i:{body})"
    return rf"(?<![A-Za-z0-9_.-]){body}(?![A-Za-z0-9_-])"


def _address(text):
    return rf"(?<![0-9A-Za-z.:]){re.escape(text)}(?![0-9A-Za-z]|\.[0-9]|:[0-9A-Fa-f]*:)"


def host_context():
    """What the host itself tells about you: HOME, git e-mail address and host name."""
    homes = []
    for home in (str(Path.home()), os.path.realpath(Path.home())):
        if home not in homes and home.count("/") >= 2:
            homes.append(home)
    return {"homes": homes, "email": host_git_value("user.email"), "hostname": socket.gethostname(),
            "addresses": [], "servers": [], "tokens": []}


def host_git_value(key):
    """KEY from the host's global git configuration, or None."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        result = subprocess.run(["git", "config", "--global", "--get", key], capture_output=True, text=True,
                                timeout=10, stdin=subprocess.DEVNULL, cwd=Path.home(), env=env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value or None


def derived_rules(context):
    """Rules for what CONTEXT (see host_context) names as yours. Tokens are matched literally and never shown."""
    rules = Rules(sources=["derived"])

    def add(id_, regex, severity, reason, secret=False):
        rules.patterns.append(Pattern(id_, re.compile(regex), severity, reason, secret))

    for home in context.get("homes", []):
        add("host-home", re.escape(home) + r"(?![A-Za-z0-9._-])", BLOCK,
            "your host HOME: it names your account and the layout of your machine")
    email = context.get("email") or ""
    if "@" in email and "noreply" not in email.lower():
        add("git-email", f"(?i:{re.escape(email)})", WARN,
            "your git e-mail address; on GitHub, a noreply address keeps it private")
    hostname = (context.get("hostname") or "").lower()
    if len(hostname) >= 4 and hostname not in GENERIC_HOSTNAMES and re.fullmatch(r"[a-z0-9.-]+", hostname):
        add("host-name", _word(hostname, case=False), WARN, "the name of your machine")
    for value in dict.fromkeys(context.get("addresses", [])):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if (address.is_private or address.is_link_local) and not address.is_loopback \
                and not any(address in network for network in DOCUMENTATION):
            add("private-address", _address(str(address)), BLOCK, "a host on your private network (--allow-tcp)")
    for server in context.get("servers", []):
        host, port, user = str(server.get("host") or ""), server.get("port"), server.get("user")
        kind = host_kind(host) if host else "public"
        if kind == "public":
            continue  # an account on a public host is a public identity
        if kind == "loopback" and port:
            add("own-git-server", rf"(?:localhost|127\.0\.0\.1|\[::1\]):{int(port)}(?![0-9])", WARN,
                "your own Git server on this machine")
        elif kind in ("private", "custom"):
            add("own-git-server", _word(host.lower(), case=False), BLOCK if kind == "private" else WARN,
                f"your own Git server {host}")
        if user and len(user) >= 3 and user.lower() not in GENERIC_USERS:
            add("own-git-account", _word(user, case=False), BLOCK, f"your account name on your Git server {host}")
    for token in dict.fromkeys(context.get("tokens", [])):
        if len(token) >= 8:
            add("own-token", re.escape(token), BLOCK, "one of your Git tokens or passwords", secret=True)
    return rules


def private_hosts(context):
    """Hosts of your own Git servers: a push there is not a publication."""
    return sorted({str(s["host"]).lower() for s in context.get("servers", [])
                   if s.get("host") and host_kind(str(s["host"])) != "public"})


def rules_document(rules):
    """RULES as a JSON-able document (for the hooks in the sandbox); secrets stay marked."""
    return {"pattern": [{"id": p.id, "regex": p.regex.pattern, "severity": p.severity, "reason": p.reason,
                         "secret": p.secret} for p in rules.patterns],
            "allow": [{"id": a.id, "regex": a.regex.pattern, "reason": a.reason,
                       **({"path_glob": a.path_glob} if a.path_glob else {})} for a in rules.allows]}


# --- remotes ---------------------------------------------------------------------------------------

def remote_host(url):
    """Host of a Git remote URL, or None for a local path or file:// URL."""
    url = url.strip()
    if "://" in url:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "file":
            return None
        try:
            return parsed.hostname
        except ValueError:
            return None
    # scp-like syntax: [user@]host:path, where host has no slash
    match = re.fullmatch(r"(?:[^@/]+@)?(\[[^\]]+\]|[^:/]+):(?!//).*", url)
    if match and not url.startswith(("/", ".")):
        return match[1].strip("[]")
    return None


def remote_is_public(url, own_hosts=()):
    """A remote outside this machine, your private network and your own Git servers."""
    host = remote_host(url)
    if not host:
        return False
    host = host.lower()
    if host in {h.lower() for h in own_hosts}:
        return False
    return host_kind(host) in ("public", "custom")


# --- git -------------------------------------------------------------------------------------------

class Git:
    """Git plumbing for one repository. On the host (HOST=True) the environment carries no GIT_* variable
    and no command starts a program the repository's configuration names."""

    def __init__(self, repo, host=True):
        self.repo, self.host = Path(os.path.abspath(repo)), host

    def __call__(self, *args, data=None, check=True):
        env = dict(os.environ)
        if self.host:
            env = {k: v for k, v in env.items() if not k.startswith("GIT_")}
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
        command = ["git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
        if self.host:
            command += ["-C", str(self.repo), "--work-tree", str(self.repo)]
        try:
            result = subprocess.run([*command, *args], input=data, capture_output=True, env=env,
                                    timeout=GIT_TIMEOUT, stdin=None if data is not None else subprocess.DEVNULL)
        except FileNotFoundError:
            raise RulesUnavailable("git is not installed") from None
        except subprocess.TimeoutExpired:
            raise RulesUnavailable(f"{self.repo}: git {args[0]} took longer than {GIT_TIMEOUT} s") from None
        if check and result.returncode != 0:
            message = result.stderr.decode(errors="replace").strip().splitlines()
            raise RulesUnavailable(f"{self.repo}: git {args[0]} failed: {message[-1] if message else result.returncode}")
        return result

    def out(self, *args, **kwargs):
        return self(*args, **kwargs).stdout


def is_repository(path):
    return os.path.lexists(Path(path) / ".git")


def remotes(git):
    """{name: [url, ...]} from the repository's configuration (url and pushurl)."""
    result = git("config", "-z", "--get-regexp", r"^remote\..*\.(url|pushurl)$", check=False)
    found = {}
    for entry in result.stdout.decode(errors="replace").split("\0"):
        key, _, value = entry.partition("\n")
        if key.startswith("remote.") and value:
            found.setdefault(key[len("remote."):].rsplit(".", 1)[0], []).append(value)
    return found


def object_hash(git):
    result = git("rev-parse", "--show-object-format", check=False)
    return hashlib.sha256 if result.stdout.strip() == b"sha256" else hashlib.sha1


def blob_id(data, hasher):
    return hasher(b"blob %d\0" % len(data) + data).hexdigest()


def tree_entries(git, *args):
    """{path: (mode, object id)} from ls-files -s (stage 0) or ls-tree -r."""
    entries = {}
    for record in git.out(*args).split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        fields = meta.split()
        if args[0] == "ls-files":
            mode, oid, stage = fields
            if stage != b"0":
                continue
        else:
            mode, _, oid = fields
        entries[path.decode(errors="surrogateescape")] = (mode.decode(), oid.decode())
    return entries


def commits(git, revisions):
    """Commit ids of REVISIONS (rev-list arguments), newest first; (ids, truncated)."""
    ids = git.out("rev-list", f"--max-count={COMMITS_LIMIT + 1}", *revisions).decode().split()
    return ids[:COMMITS_LIMIT], len(ids) > COMMITS_LIMIT


def commit_metadata(git, ids):
    """[(id, author, committer, message)] of IDS."""
    if not ids:
        return []
    raw = git.out("log", "--no-walk=unsorted", "--stdin", "--no-show-signature",
                  "--format=%H%x00%an <%ae>%x00%cn <%ce>%x00%B%x00", data="\n".join(ids).encode() + b"\n")
    fields = raw.decode(errors="replace").split("\0")
    return [(fields[i].strip(), fields[i + 1], fields[i + 2], fields[i + 3])
            for i in range(0, len(fields) - 3, 4)]


def lines_of(contents, oid):
    """The lines of blob OID in CONTENTS, as scan_text's KNOWN."""
    text = decode(contents.get(oid, b"")) if oid else None
    return frozenset(text.split("\n")) if text else frozenset()


def changed_blobs(git, ids):
    """[(blob id, previous blob id or None, path, commit id)] that the commits IDS add or change
    (no merges, no submodules)."""
    if not ids:
        return []
    raw = git.out("diff-tree", "--stdin", "-r", "-z", "--root", "--no-renames",
                  data="\n".join(ids).encode() + b"\n")
    tokens, found, current, index = raw.split(b"\0"), [], None, 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith(b":"):
            fields = token[1:].split()
            path = tokens[index + 1].decode(errors="surrogateescape") if index + 1 < len(tokens) else ""
            index += 2
            if len(fields) >= 5 and fields[4][:1] in (b"A", b"M", b"T") and fields[1] != b"160000":
                before = None if ZERO.fullmatch(fields[2].decode()) or fields[0] == b"160000" else fields[2].decode()
                found.append((fields[3].decode(), before, path, current))
            continue
        if token.strip():
            current = token.strip().decode()
        index += 1
    return found


def read_blobs(git, ids, budget):
    """{blob id: bytes} for IDS up to FILE_LIMIT each; (contents, skipped ids). BUDGET is a [bytes left] list."""
    ids = list(dict.fromkeys(ids))
    if not ids:
        return {}, []
    sizes = {}
    for line in git.out("cat-file", "--batch-check", data="\n".join(ids).encode() + b"\n").splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == b"blob":
            sizes[parts[0].decode()] = int(parts[2])
    wanted, skipped = [], []
    for oid in ids:
        size = sizes.get(oid)
        if size is None:
            continue
        if size > FILE_LIMIT or size > budget[0]:
            skipped.append(oid)
            continue
        budget[0] -= size
        wanted.append(oid)
    contents = {}
    if wanted:
        raw, position = git.out("cat-file", "--batch", data="\n".join(wanted).encode() + b"\n"), 0
        while position < len(raw):
            end = raw.index(b"\n", position)
            header = raw[position:end].split()
            size = int(header[2])
            contents[header[0].decode()] = raw[end + 1:end + 1 + size]
            position = end + 1 + size + 1
    return contents, skipped


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def read_in(root_fd, relative, limit):
    """(bytes, None) of the regular file ROOT/RELATIVE, else (None, why); no component is followed as a symlink,
    so a link the agent planted cannot lead out of the workspace."""
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None, "invalid path"
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                following = os.open(part, DIR_FLAGS, dir_fd=fd)
            except OSError:
                return None, "below a symlink or missing"
            os.close(fd)
            fd = following
        try:
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
                              dir_fd=fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return None, "symlink"
            return None, "missing" if exc.errno == errno.ENOENT else exc.strerror
        with os.fdopen(file_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None, "not a regular file"
            if info.st_size > limit:
                return None, "too large"
            return stream.read(limit + 1), None
    finally:
        os.close(fd)


# --- scanning --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    where: str
    path: str
    pattern: Pattern
    line: str

    def render(self, rules):
        return f"{self.where} [{self.pattern.id}] {self.pattern.severity}: {self.pattern.reason} | " \
               f"{shown(self.line, rules)}"


def shown(line, rules):
    """LINE as it may be printed: every secret match masked, control characters dropped, shortened."""
    for pattern in rules.patterns:
        if pattern.secret:
            line = pattern.regex.sub(f"[{pattern.id}]", line)
    line = "".join(c if c.isprintable() else " " for c in line.strip())
    return line if len(line) <= TEXT_WIDTH else line[:TEXT_WIDTH - 1] + "…"


def decode(data):
    """Text of DATA, or None for a binary file."""
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def scan_text(text, rules, path, where, known=frozenset()):
    """Findings in TEXT; WHERE(line number) names the place. Lines in KNOWN, those of the version before,
    are skipped: they are published already, or reported where that version is checked."""
    findings = []
    for number, line in enumerate(text.split("\n"), 1):
        if not line or line in known or any(allow.covers(path, line) for allow in rules.allows):
            continue
        for pattern in rules.patterns:
            if pattern.regex.search(line):
                findings.append(Finding(where(number), path, pattern, line))
    return findings


@dataclass
class Report:
    repo: str
    findings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    scanned: int = 0
    _seen: set = field(default_factory=set, repr=False)

    def add(self, findings):
        for finding in findings:
            key = (finding.path, finding.pattern.id, finding.line.strip())
            if key not in self._seen:
                self._seen.add(key)
                self.findings.append(finding)

    @property
    def blocking(self):
        return [f for f in self.findings if f.pattern.severity == BLOCK]


def scan_commits(git, rules, revisions, report, budget):
    """Author, committer, message and changed files of the commits REVISIONS selects."""
    ids, truncated = commits(git, revisions)
    if truncated:
        report.notes.append(f"only the newest {COMMITS_LIMIT} commits were checked")
    for oid, author, committer, message in commit_metadata(git, ids):
        short = oid[:10]
        labels = {1: f"commit {short} author", 2: f"commit {short} committer"}
        text = f"{author}\n{committer}\n{message}"
        report.add(scan_text(text, rules, "", lambda n, s=short, l=labels: l.get(n, f"commit {s} message")))
    changes = changed_blobs(git, ids)
    contents, skipped = read_blobs(git, [oid for change in changes for oid in change[:2] if oid], budget)
    seen = set()
    for oid, before, path, commit in changes:
        if (oid, before) in seen:
            continue
        seen.add((oid, before))
        if oid in skipped:
            report.notes.append(f"{path} (commit {commit[:10]}) not checked: larger than {FILE_LIMIT >> 20} MiB "
                                "or over the size budget")
            continue
        text = decode(contents.get(oid, b""))
        if text is None:
            continue
        report.scanned += 1
        report.add(scan_text(text, rules, path, lambda n, p=path, c=commit[:10]: f"{p}:{n} (commit {c})",
                             lines_of(contents, before)))
    return ids


def staged_changes(git):
    """{path: (blob id in the index, blob id in HEAD or None)} of what the next commit changes."""
    has_head = git("rev-parse", "--verify", "-q", "HEAD^{commit}", check=False).returncode == 0
    index = tree_entries(git, "ls-files", "-s", "-z")
    head = tree_entries(git, "ls-tree", "-r", "-z", "--full-tree", "HEAD") if has_head else {}
    return index, {path: (oid, head[path][1] if path in head and head[path][0] != "160000" else None)
                   for path, (mode, oid) in index.items()
                   if mode != "160000" and head.get(path, (None, None))[1] != oid}


def scan_staged(git, rules, report, budget, label=" (staged)"):
    index, staged = staged_changes(git)
    contents, skipped = read_blobs(git, [oid for pair in staged.values() for oid in pair if oid], budget)
    for path, (oid, before) in staged.items():
        text = decode(contents.get(oid, b"")) if oid not in skipped else None
        if text is not None:
            report.scanned += 1
            report.add(scan_text(text, rules, path, lambda n, p=path: f"{p}:{n}{label}", lines_of(contents, before)))
    return index


def scan_repository(repo, rules, everything=False):
    """What the next push of REPO would publish, on the host (see the module docstring).
    EVERYTHING: every tracked and untracked file of the work tree, whatever changed."""
    repo = Path(repo)
    git = Git(repo)
    if not is_repository(repo):
        raise RulesUnavailable(f"{repo} is not a Git repository")
    git("rev-parse", "--git-dir")
    report, budget = Report(str(repo)), [TOTAL_LIMIT]
    scan_commits(git, rules, ["--branches", "--not", "--remotes"], report, budget)
    index = scan_staged(git, rules, report, budget)
    hasher = object_hash(git)
    untracked = [p for p in git.out("ls-files", "-z", "-o", "--exclude-standard").decode(
        errors="surrogateescape").split("\0") if p]
    candidates = [(path, index[path][1]) for path in index if index[path][0] not in ("160000", "120000")]
    candidates += [(path, None) for path in untracked]
    if len(candidates) > FILES_LIMIT:
        report.notes.append(f"only {FILES_LIMIT} of {len(candidates)} work tree files were checked")
    changed = []
    root = os.open(repo, DIR_FLAGS)
    try:
        for path, oid in candidates[:FILES_LIMIT]:
            if budget[0] <= 0:
                report.notes.append("size budget used up; the rest of the work tree was not checked")
                break
            data, why = read_in(root, path, FILE_LIMIT)
            if data is None:
                if why == "too large":
                    report.notes.append(f"{path} not checked: larger than {FILE_LIMIT >> 20} MiB")
                continue
            budget[0] -= len(data)
            if oid is not None and not everything and blob_id(data, hasher) == oid:
                continue  # as in the index: covered by the commits and the staged changes
            changed.append((path, oid, data))
    finally:
        os.close(root)
    # A changed file is checked for what is new against its version in the index.
    contents = {} if everything else read_blobs(git, [oid for _, oid, _ in changed if oid], budget)[0]
    for path, oid, data in changed:
        text = decode(data)
        if text is None:
            continue
        report.scanned += 1
        label = "untracked" if oid is None else "not staged"
        report.add(scan_text(text, rules, path, lambda n, p=path, l=label: f"{p}:{n} ({l})",
                             lines_of(contents, oid)))
    return report


def format_report(report, rules, limit=SHOWN):
    lines = [finding.render(rules) for finding in report.findings[:limit]]
    if len(report.findings) > limit:
        lines.append(f"... and {len(report.findings) - limit} more")
    return lines + [f"note: {note}" for note in report.notes[:limit]]


def summary(report):
    blocking = len(report.blocking)
    warnings = len(report.findings) - blocking
    return f"{blocking} blocking, {warnings} warning(s)"


# --- after a run (called by the launcher) ----------------------------------------------------------

def check_after_run(workspaces, rules_for, own_hosts, mode, out):
    """Report findings of every Git workspace in WORKSPACES ([(host path, project name)]); MODE "public"
    checks only workspaces with a public remote, "always" every one. RULES_FOR(project) gives the rules.
    OUT(line) prints. Never raises: a failed check is reported, the run's exit status is kept."""
    for path, project in workspaces:
        try:
            if not is_repository(path):
                continue
            git = Git(path)
            public = [url for urls in remotes(git).values() for url in urls if remote_is_public(url, own_hosts)]
            if mode == "public" and not public:
                continue
            rules = rules_for(project)
            report = scan_repository(path, rules)
            if not report.findings:
                continue
            target = f" (remote {remote_host(public[0])})" if public else ""
            out(f"{path}{target}: {summary(report)} in what the next push would publish:")
            for line in format_report(report, rules):
                out(f"  {line}")
            out(f"check again with: sandbox-ai privacy-check {path}")
        except Exception as exc:  # noqa: BLE001 - whatever the agent left in the repository
            out(f"{path}: could not check: {exc}")


# --- hooks in the sandbox --------------------------------------------------------------------------

HOOK_NAMES = ("pre-commit", "pre-push")
HOOK_SCRIPT = """#!/bin/sh
# sandbox-ai privacy check; afterwards the repository's own {name} hook runs, if there is one.
exec python3 -I {engine} hook {name} "$@"
"""


def write_hooks(directory, engine_path, rules, own_hosts):
    """DIRECTORY/{privacy.py, hook.json, hooks/pre-commit, hooks/pre-push} for the sandbox, which sees
    DIRECTORY as ENGINE_PATH's parent."""
    hooks = directory / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (directory / "privacy.py").write_bytes(Path(__file__).read_bytes())
    (directory / "hook.json").write_text(json.dumps({"rules": rules_document(rules), "own_hosts": own_hosts}))
    for name in HOOK_NAMES:
        script = hooks / name
        script.write_text(HOOK_SCRIPT.format(name=name, engine=engine_path))
        script.chmod(0o755)
    for path in (directory / "privacy.py", directory / "hook.json"):
        path.chmod(0o644)
    for path in (directory, hooks):
        path.chmod(0o755)


def own_hook(git, name):
    """The repository's own hook NAME (core.hooksPath of the repository or the user, else .git/hooks)."""
    configured = None
    for scope in ("--local", "--global"):
        result = git("config", scope, "--get", "core.hooksPath", check=False)
        if result.returncode == 0 and result.stdout.strip():
            configured = result.stdout.decode().strip()
            break
    if configured:
        directory = Path(os.path.expanduser(configured))
        if not directory.is_absolute():
            directory = Path(git.out("rev-parse", "--show-toplevel").decode().strip()) / directory
    else:
        # Not --git-path hooks: that answers with core.hooksPath, which is ours in the sandbox.
        directory = Path(git.out("rev-parse", "--git-common-dir").decode().strip()) / "hooks"
    hook = directory / name
    if hook.resolve().parent == HERE / "hooks":
        return None
    return hook if hook.is_file() and os.access(hook, os.X_OK) else None


def run_hook(name, args, stdin=sys.stdin.buffer, err=sys.stderr):
    """The hook NAME in the sandbox: pre-commit warns, pre-push to a public remote refuses blocking findings."""
    git = Git(".", host=False)
    data = stdin.read() if name == "pre-push" else b""
    try:
        config = json.loads((HERE / "hook.json").read_text())
        rules = builtin_rules().add(parse_rules(config["rules"], "derived"))
        status = (check_staged if name == "pre-commit" else check_push)(git, rules, args, data, config, err)
    except Exception as exc:  # noqa: BLE001 - a check that cannot run must not wave a push through
        print(f"sandbox-ai privacy: the check failed: {exc}", file=err)
        status = 1 if name == "pre-push" and args[1:2] and remote_is_public(args[1]) else 0
    if status:
        return status
    hook = own_hook(git, name)
    if hook is None:
        return 0
    return subprocess.run([str(hook), *args], input=data).returncode


def check_staged(git, rules, args, data, config, err):
    report = Report(".")
    scan_staged(git, rules, report, [TOTAL_LIMIT], label="")
    if report.findings:
        print("sandbox-ai privacy: the staged changes hold what should not become public:", file=err)
        for line in format_report(report, rules):
            print(f"  {line}", file=err)
        print("The commit goes ahead; a push of it to a public remote will be refused. Remove these, or ask "
              "the user whether they are meant to be published.", file=err)
    return 0


def check_push(git, rules, args, data, config, err):
    url = args[1] if len(args) > 1 else ""
    if not remote_is_public(url, config.get("own_hosts", [])):
        return 0
    local, remote = [], []
    for line in data.decode(errors="replace").splitlines():
        fields = line.split()
        if len(fields) != 4 or ZERO.fullmatch(fields[1]):
            continue  # a deletion publishes nothing
        local.append(fields[1])
        if not ZERO.fullmatch(fields[3]) and git("cat-file", "-e", f"{fields[3]}^{{commit}}",
                                                 check=False).returncode == 0:
            remote.append(fields[3])
    if not local:
        return 0
    report = Report(".")
    scan_commits(git, rules, [*local, "--not", "--remotes", *remote], report, [TOTAL_LIMIT])
    if not report.findings:
        return 0
    blocking = bool(report.blocking)
    print(f"sandbox-ai privacy: this push to {remote_host(url)} would publish "
          f"{'what must stay private' if blocking else 'what may be private'}:", file=err)
    for line in format_report(report, rules):
        print(f"  {line}", file=err)
    if blocking:
        print("Push refused. Remove these from the commits (rewrite them before pushing), or ask the user "
              "whether they are meant to be published.", file=err)
        return 1
    return 0


# --- command line ----------------------------------------------------------------------------------

def project_context(path, notes):
    """Host values plus those of PATH's trusted project configuration (.sandbox-ai.json)."""
    context = host_context()
    config = Path(path) / ".sandbox-ai.json"
    if not config.is_file():
        return context, None
    try:
        spec = importlib.util.spec_from_file_location("sandbox_agent_sandbox", HERE / "agent_sandbox.py")
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        agent = json.loads(config.read_text()).get("agent")
        argv = [agent, "--workspace", str(path)]
        a = launcher.parse_args(argv)
        _, document = launcher.load_project_config(a)
        if document:
            a = launcher.parse_args(launcher.merge_config(argv, a, document))
        derived = launcher.privacy_context(a)
        return derived, a.privacy_rules
    except Exception as exc:  # noqa: BLE001 - untrusted, missing variables, another layout
        notes.append(f"{config}: not used ({exc}); only the host's own values are derived")
        return context, None


def check_command(argv):
    parser = argparse.ArgumentParser(prog="sandbox-ai privacy-check",
                                     description="Show what the next push of a repository would publish that "
                                                 "should stay private.")
    parser.add_argument("paths", nargs="*", default=["."], metavar="PATH", help="Git repositories (default: .)")
    parser.add_argument("--rules-dir", type=Path, help="your own rules (default: --privacy-rules of the project, "
                                                       f"else {default_rules_dir()} if it exists)")
    parser.add_argument("--project", help="name of the project rule file (default: the directory name)")
    parser.add_argument("--all", action="store_true", help="every tracked and untracked file, also unchanged ones")
    parser.add_argument("--json", action="store_true", help="findings as JSON")
    args = parser.parse_args(argv)
    status, results = EXIT_CLEAN, []
    for value in args.paths:
        path = Path(value).expanduser().resolve()
        notes = []
        try:
            context, configured = project_context(path, notes)
            rules_dir = args.rules_dir or configured or (default_rules_dir() if default_rules_dir().is_dir() else None)
            rules = builtin_rules().add(derived_rules(context)).add(user_rules(rules_dir, args.project or path.name))
            report = scan_repository(path, rules, everything=args.all)
        except RulesUnavailable as exc:
            print(f"sandbox-ai privacy-check: REFUSED: {exc}", file=sys.stderr)
            status = max(status, EXIT_REFUSED)
            continue
        report.notes[:0] = notes
        if report.blocking:
            status = max(status, EXIT_FINDINGS)
        if args.json:
            results.append({"repository": str(path), "rules": rules.sources, "scanned": report.scanned,
                            "notes": report.notes,
                            "findings": [{"where": f.where, "path": f.path, "id": f.pattern.id,
                                          "severity": f.pattern.severity, "reason": f.pattern.reason,
                                          "line": shown(f.line, rules)} for f in report.findings]})
            continue
        print(f"{path}: {summary(report)} (rules: {', '.join(rules.sources)}; {report.scanned} file(s) checked)")
        for line in format_report(report, rules, limit=len(report.findings)):
            print(f"  {line}")
    if args.json:
        print(json.dumps(results, indent=2))
    return status


def main(argv):
    if argv[:1] == ["hook"] and len(argv) >= 2 and argv[1] in HOOK_NAMES:
        return run_hook(argv[1], argv[2:])
    if argv[:1] == ["check"]:
        return check_command(argv[1:])
    return check_command(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
