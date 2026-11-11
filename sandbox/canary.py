#!/usr/bin/env python3
"""sandbox-ai canaries: decoy files with tokens made for each run.

  sandbox-ai canaries list [RUN] [--tokens] [--json]   runs with canaries; one run's files and what happened
  sandbox-ai canaries log [RUN] [--json]               every event: when, which canary, by which process, and
                                                       the agent's tool call, prompt and model requests around it
  sandbox-ai canaries resume RUN                       continue a sandbox a canary paused

A sandbox gets decoy files (canaries) that look like credentials: ~/.aws/credentials, a gcloud login, an SSH
key, a gh login, ~/.netrc, ~/.git-credentials (category credentials, the default); a password list and a
Kubernetes config (home); a .env in each workspace (workspace). Their tokens are made for the run, have the
shape of real ones and are worthless: no service accepts them (AWS keys in the documented EXAMPLE form, a
GitHub token with a wrong checksum, random values for hosts under .internal). Nothing in them is yours, and
nothing is planted over a file that exists.

When something opens or reads one, the supervisor (inotify) reports it; when a token leaves the sandbox (the
connection proxy sees it in a request's URL, headers or body, plain or URL-, base64-, hex- or JSON-encoded,
in the name of a TLS connection or a DNS lookup, or the push check finds it in pushed commits), that is the
stronger signal. Rulesets decide what follows ("canary": {"on_read": "report|ask|pause|stop", "on_send":
"report|block|pause|stop"}; defaults: report a read, block the request and stop the sandbox when a token
leaves); the strictest ruleset wins. Every event lands in canaries.jsonl in the log directory and in
`sandbox-ai canaries list RUN`.

The list of canaries and their tokens never enter the sandbox where the agent could read them: the launcher
keeps them in your state directory, the supervisor and the proxy get them through directories hidden from
the agent.

Standard library only.
"""
import argparse
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import zlib

HERE = Path(__file__).resolve().parent

CATEGORIES = ("credentials", "home", "workspace")
DEFAULT_CATEGORIES = ("credentials", "home")
ON_READ = ("report", "ask", "pause", "stop")         # in rising strictness, as in the rulesets (runtime.py)
ON_SEND = ("report", "block", "pause", "stop")
DEFAULT_DECISIONS = {"on_read": "report", "on_send": "stop"}
ASK_SECONDS = 300           # a question about a read waits this long; the sandbox stays paused after that
NOTIFY_REPEAT = 60          # seconds between notifications about the same canary and kind of event
STATE_DAYS = 90             # how long the record of an ended run is kept
EVENTS_LIMIT = 16 << 20     # bytes of events kept per run
CONFIG_NAME = "canary.json"            # in the run's control directory (CONTROL_DIR in the sandbox)
EVENTS_NAME = "canary-events.jsonl"    # what the supervisor and the proxy report, there too
RESUME_NAME = "canary-resume"          # `sandbox-ai canaries resume` asks the launcher to continue
LOG_NAME = "canaries.jsonl"            # in the log directory
EXCLUDE_NOTE = "# sandbox-ai canary ({run}): planted for the run, removed when it ends"
RUN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}")

UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LOWER = UPPER.lower()
DIGITS = "0123456789"
ALNUM = UPPER + LOWER + DIGITS
BASE64 = ALNUM + "+/"
URLSAFE = ALNUM + "-_"
WORDS = ("atlas", "falcon", "harbor", "juniper", "nimbus", "orion", "quartz", "summit", "tundra", "vertex")
USERS = ("deploy", "ci-bot", "release", "ops-admin", "build", "svc-backup")


# --- tokens ---------------------------------------------------------------------------------------

def rand(alphabet, count):
    return "".join(secrets.choice(alphabet) for _ in range(count))


def base62(number, alphabet):
    text = ""
    while number:
        number, digit = divmod(number, 62)
        text = alphabet[digit] + text
    return text.rjust(6, alphabet[0])


def github_token():
    """ghp_ + 30 characters + a 6-character checksum that is wrong in both alphabets in use: secret scanners
    take it for a GitHub token, GitHub knows it is none without a lookup."""
    body = rand(ALNUM, 30)
    crc = zlib.crc32(body.encode())
    valid = {base62(crc, DIGITS + UPPER + LOWER), base62(crc, DIGITS + LOWER + UPPER)}
    checksum = rand(ALNUM, 6)
    while checksum in valid:
        checksum = rand(ALNUM, 6)
    return "ghp_" + body + checksum


def aws_key_id():
    """An access key id in the form of AWS's documented example (AKIA...EXAMPLE)."""
    return "AKIA" + rand(UPPER + "234567", 9) + "EXAMPLE"


def aws_secret():
    return rand(BASE64, 30) + "EXAMPLEKEY"


def ssh_private_key():
    """(text, token): an OpenSSH private key's armour around base64 that starts like an unencrypted ed25519 key
    and holds the token in its second line; it does not decode to a key."""
    token = rand(BASE64, 48)
    lines = ["b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW",
             rand(BASE64, 11) + token + rand(BASE64, 11), rand(BASE64, 70), rand(BASE64, 70), rand(BASE64, 18) + "=="]
    armour = "OPENSSH " + "PRIVATE KEY"  # assembled here: no scanner takes this source for a key
    return f"-----BEGIN {armour}-----\n" + "\n".join(lines) + f"\n-----END {armour}-----\n", token


def jwt_like():
    """(token text, signature): a service-account token's shape; the signature is random."""
    def part(document):
        return base64.urlsafe_b64encode(json.dumps(document, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = rand(URLSAFE, 86)
    claims = {"iss": "kubernetes/serviceaccount", "kubernetes.io/serviceaccount/namespace": "kube-system",
              "kubernetes.io/serviceaccount/secret.name": f"admin-token-{rand(LOWER + DIGITS, 5)}"}
    return f"{part({'alg': 'RS256', 'kid': rand(URLSAFE, 43)})}.{part(claims)}.{signature}", signature


def internal_host(kind):
    return f"{kind}.{secrets.choice(WORDS)}.internal"


# --- what is planted ------------------------------------------------------------------------------

def credential_files():
    """The canaries of category credentials: [{"id", "category", "path" (in HOME), "content", "tokens", "reads",
    "describe"}]. "reads": alert, or log for files that ordinary tools open on their own (those reads are
    logged only; a token of theirs leaving still counts)."""
    user, project = secrets.choice(USERS), f"{secrets.choice(WORDS)}-prod-{rand(DIGITS, 6)}"
    key_id, secret = aws_key_id(), aws_secret()
    profile = secrets.choice(("prod-admin", "deploy", "billing", "terraform-prod"))
    client_secret, refresh = "GOCSPX-" + rand(URLSAFE, 28), "1//0" + rand(URLSAFE, 60)
    account = f"{user}@{project}.internal"
    key, key_token = ssh_private_key()
    gh_host, gh_token = internal_host("github"), github_token()
    netrc_host, netrc_password = internal_host("artifacts"), rand(ALNUM, 24)
    git_host, git_token = internal_host("git"), "glpat-" + rand(URLSAFE, 20)
    return [
        {"id": "aws", "path": ".aws/credentials", "reads": "alert", "describe": f"AWS keys (profile {profile})",
         "tokens": {"aws_access_key_id": key_id, "aws_secret_access_key": secret},
         "content": f"[{profile}]\naws_access_key_id = {key_id}\naws_secret_access_key = {secret}\n"},
        {"id": "gcloud", "path": f".config/gcloud/legacy_credentials/{account}/adc.json", "reads": "alert",
         "describe": f"a gcloud login ({account})",
         "tokens": {"client_secret": client_secret, "refresh_token": refresh},
         "content": json.dumps({"client_id": f"{rand(DIGITS, 12)}-{rand(LOWER + DIGITS, 32)}.apps.googleusercontent"
                                             ".com", "client_secret": client_secret, "refresh_token": refresh,
                                "type": "authorized_user"}, indent=2) + "\n"},
        {"id": "ssh", "path": f".ssh/id_ed25519_{secrets.choice(('prod', 'deploy', 'backup'))}", "reads": "alert",
         "describe": "an SSH private key", "tokens": {"key": key_token}, "content": key},
        {"id": "gh", "path": ".config/gh/hosts.yml", "reads": "log", "describe": f"a gh login ({gh_host})",
         "tokens": {"oauth_token": gh_token},
         # github.com first, without a login: gh takes the first host of hosts.yml as its default, and a sandbox
         # given a real token (GH_TOKEN for github.com) sent its gh calls to the decoy's host instead.
         "content": f"github.com:\n    git_protocol: https\n"
                    f"{gh_host}:\n    users:\n        {user}:\n            oauth_token: {gh_token}\n"
                    f"    git_protocol: https\n    oauth_token: {gh_token}\n    user: {user}\n"},
        {"id": "netrc", "path": ".netrc", "reads": "log", "describe": f"a .netrc login ({netrc_host})",
         "tokens": {"password": netrc_password},
         "content": f"machine {netrc_host}\n  login {user}\n  password {netrc_password}\n"},
        {"id": "git-credentials", "path": ".git-credentials", "reads": "alert",
         "describe": f"a Git login ({git_host})", "tokens": {"token": git_token},
         "content": "https://oauth2" + ":" + git_token + "@" + git_host + "\n"},
    ]


def home_files():
    """The canaries of category home: a password list and a Kubernetes configuration."""
    first, second = rand(ALNUM, 20), rand(ALNUM + "!#%", 18)
    token, signature = jwt_like()
    server = internal_host("k8s")
    return [
        {"id": "passwords", "path": "Documents/passwords.txt", "reads": "alert", "describe": "a password list",
         "tokens": {"vpn": first, "database": second},
         "content": f"vpn ({internal_host('vpn')}): {secrets.choice(USERS)} / {first}\n"
                    f"prod database: admin / {second}\n"},
        {"id": "kube", "path": ".kube/config", "reads": "alert", "describe": f"a Kubernetes login ({server})",
         "tokens": {"token": signature},
         "content": f"apiVersion: v1\nkind: Config\nclusters:\n- cluster:\n    server: https://{server}:6443\n"
                    f"  name: prod\ncontexts:\n- context:\n    cluster: prod\n    user: admin\n  name: prod\n"
                    f"current-context: prod\nusers:\n- name: admin\n  user:\n    token: {token}\n"},
    ]


def workspace_file(number):
    """The canary of category workspace for workspace NUMBER (0: /work): a .env whose names no framework reads."""
    database, key = rand(ALNUM, 22), "sk_" + rand(ALNUM, 32)
    return {"id": "env" if number == 0 else f"env-{number + 1}", "path": ".env", "reads": "alert",
            "describe": "a .env with a database password and an API key",
            "tokens": {"PROD_DATABASE_URL": database, "DEPLOY_API_KEY": key},
            "content": f"# production (do not commit)\nPROD_DATABASE_URL=postgres://app:{database}@"
                       f"{internal_host('db')}:5432/app\nDEPLOY_API_KEY={key}\n"}


# --- the forms of a token in traffic --------------------------------------------------------------

def forms(token):
    """{text: encoding}: TOKEN as a request may carry it: plain, URL-encoded (either case), JSON-escaped, hex,
    and base64 (standard and URL-safe) at each of the three offsets a token can have in an encoded stream."""
    found = {}

    def add(text, encoding):
        if len(text) >= 8:
            found.setdefault(text, encoding)
    add(token, "plain")
    for safe in ("", "/"):
        quoted = urllib.parse.quote(token, safe=safe)
        add(quoted, "url")
        add(re.sub(r"%[0-9A-F]{2}", lambda match: match.group().lower(), quoted), "url")
    add(urllib.parse.quote_plus(token), "url")
    add(json.dumps(token)[1:-1], "json")
    add(token.replace("/", "\\/"), "json")
    data = token.encode()
    for skip in range(3):
        piece = data[skip:]
        encoded = base64.b64encode(piece[:len(piece) // 3 * 3]).decode()
        add(encoded, "base64")
        add(encoded.replace("+", "-").replace("/", "_"), "base64url")
    add(data.hex(), "hex")
    add(data.hex().upper(), "hex")
    return found


def name_forms(token):
    """{text: encoding}: TOKEN as it may hide in a host name (lower case, the dots taken out): itself where its
    characters fit, hex, base32 at each of five offsets, URL-safe base64 at each of three."""
    found = {}

    def add(text, encoding):
        text = text.lower()
        if len(text) >= 8 and re.fullmatch(r"[a-z0-9_-]+", text):
            found.setdefault(text, encoding)
    add(token, "plain")
    data = token.encode()
    add(data.hex(), "hex")
    for skip in range(5):
        piece = data[skip:]
        add(base64.b32encode(piece[:len(piece) // 5 * 5]).decode(), "base32")
    for skip in range(3):
        piece = data[skip:]
        add(base64.urlsafe_b64encode(piece[:len(piece) // 3 * 3]).decode(), "base64url")
    return found


# --- choices --------------------------------------------------------------------------------------

def parse_categories(value, where="--canaries"):
    """The categories VALUE names ("credentials,workspace", "all", "none" or a list), in CATEGORIES' order."""
    if value is False:
        return ()
    items = value if isinstance(value, list) else str(value).split(",")
    names = {item.strip() for item in items if isinstance(item, str) and item.strip()}
    if names == {"all"}:
        return CATEGORIES
    if names == {"none"} or (not names and isinstance(value, list)):
        return ()
    unknown = sorted(names - set(CATEGORIES))
    if unknown or not names or not all(isinstance(item, str) for item in items):
        raise ValueError(f"{where}: categories are {', '.join(CATEGORIES)} (comma-separated), all or none")
    return tuple(name for name in CATEGORIES if name in names)


def categories(a):
    """The categories of this run: --canaries/--no-canaries (also from the project file), else "canaries" in
    defaults.json, else credentials. The policy's require_canaries (true: the default ones, or a list) adds what
    it requires and refuses --no-canaries; the security mode's categories (modes.py) are planted at least. None for
    probes and MCP server sandboxes."""
    if getattr(a, "probe", False) or getattr(a, "mcp_socket_dir", None):
        return ()
    chosen = getattr(a, "canaries", None)
    default = getattr(a, "canary_default", None)
    if chosen is not None:
        result = parse_categories(chosen)
    elif default is not None:
        result = parse_categories(default, "defaults.json: canaries")
    else:
        result = DEFAULT_CATEGORIES
    floor = (getattr(a, "mode_canaries", None) or {}).get("categories")
    if floor:  # the security mode's (modes.py): at least these; the strict modes refuse --no-canaries
        result = tuple(name for name in CATEGORIES if name in result or name in floor)
    required = getattr(a, "require_canaries", None)
    if required:
        needed = DEFAULT_CATEGORIES if required is True else parse_categories(required, "policy: require_canaries")
        if chosen is not None and not result:
            raise ValueError("--no-canaries: the administrator's policy requires canary files (require_canaries)")
        result = tuple(name for name in CATEGORIES if name in result or name in needed)
    return result


def check_policy_value(value):
    """True if VALUE is a valid require_canaries: true, false or a list of categories."""
    if isinstance(value, bool):
        return True
    try:
        return isinstance(value, list) and bool(parse_categories(value))
    except ValueError:
        return False


def decisions(rulesets):
    """{"on_read", "on_send"}: the strictest the rulesets (load_rulesets: {"name", ..., "canary"}) choose, the
    default where none does; "by": which ruleset decided each."""
    result, by = dict(DEFAULT_DECISIONS), {}
    for key, order in (("on_read", ON_READ), ("on_send", ON_SEND)):
        chosen = [(source["canary"][key], source.get("name", "rules")) for source in rulesets
                  if isinstance(source.get("canary"), dict) and source["canary"].get(key) in order]
        if chosen:
            result[key], by[key] = max(chosen, key=lambda item: order.index(item[0]))
    return {**result, "by": by}


# --- files, without following links ----------------------------------------------------------------

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def open_parent(root, relative, create=False, created=None):
    """A descriptor of the directory that holds RELATIVE under ROOT, reached without following a link (an agent
    may have left one in a kept HOME or a workspace); missing directories are made (0700) with CREATE and
    named in CREATED. OSError otherwise."""
    fd = os.open(root, DIRECTORY)
    parts = PurePosixPath(relative).parts
    try:
        for index, part in enumerate(parts[:-1]):
            try:
                following = os.open(part, DIRECTORY, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                if created is not None:
                    created.append("/".join(parts[:index + 1]))
                following = os.open(part, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_file(root, relative, limit=1 << 20):
    """(bytes, stat) of the regular file RELATIVE under ROOT, or None when it is not there (or is no file)."""
    try:
        parent = open_parent(root, relative)
    except OSError:
        return None
    try:
        fd = os.open(PurePosixPath(relative).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
                     dir_fd=parent)
    except OSError:
        return None
    finally:
        os.close(parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        with open(fd, "rb", closefd=False) as stream:
            return stream.read(limit), info
    finally:
        os.close(fd)


def exists(root, relative):
    """Whether anything (a file, a directory, a link) is at RELATIVE under ROOT, or it cannot be told."""
    try:
        parent = open_parent(root, relative)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # a link or no permission on the way: never plant there
    try:
        os.stat(PurePosixPath(relative).name, dir_fd=parent, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True
    finally:
        os.close(parent)


def create_file(root, relative, content, age_days=None):
    """Write CONTENT as a new file RELATIVE under ROOT (0600, never over anything); its directories are made as
    needed. {"dev", "ino", "sha256", "created"}; FileExistsError or OSError otherwise."""
    created = []
    parent = open_parent(root, relative, create=True, created=created)
    name = PurePosixPath(relative).name
    data = content.encode()
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600,
                     dir_fd=parent)
        try:
            os.write(fd, data)
            info = os.fstat(fd)
        finally:
            os.close(fd)
        # Not new-looking: some weeks or months old.
        then = time.time() - 86400 * (age_days if age_days is not None else 20 + secrets.randbelow(300))
        os.utime(name, (then, then), dir_fd=parent, follow_symlinks=False)
    except BaseException:
        os.close(parent)
        remove_dirs(root, created)
        raise
    os.close(parent)
    return {"dev": info.st_dev, "ino": info.st_ino, "sha256": hashlib.sha256(data).hexdigest(), "created": created}


def remove_file(root, relative, entry, force_same_inode=False, dirs=True):
    """Remove the canary RELATIVE under ROOT if it is still what was planted (same content, or the same file
    with FORCE_SAME_INODE); then the directories made for it, when empty. "removed", "changed" or "gone"."""
    found = read_file(root, relative)
    if found is None:
        state = "gone"
    else:
        data, info = found
        same = hashlib.sha256(data).hexdigest() == entry.get("sha256") or (
            force_same_inode and (info.st_dev, info.st_ino) == (entry.get("dev"), entry.get("ino")))
        state = "changed"
        if same:
            try:
                parent = open_parent(root, relative)
                try:
                    os.unlink(PurePosixPath(relative).name, dir_fd=parent)
                finally:
                    os.close(parent)
                state = "removed"
            except OSError:
                state = "changed"
    if state != "changed" and dirs:
        remove_dirs(root, entry.get("created", []))
    return state


def remove_dirs(root, created):
    """Remove the directories CREATED (relative to ROOT) when they are empty, deepest first."""
    for relative in sorted(created, key=lambda p: p.count("/"), reverse=True):
        with contextlib.suppress(OSError):
            parent = open_parent(root, relative)
            try:
                os.rmdir(PurePosixPath(relative).name, dir_fd=parent)
            finally:
                os.close(parent)


def rewrite(root, relative, change):
    """Replace the regular file RELATIVE under ROOT (missing: empty) with CHANGE(old text), atomically and
    without following links; False if it could not."""
    found = read_file(root, relative)
    if found is None and exists(root, relative):
        return False
    old = found[0].decode("utf-8", "replace") if found else ""
    new = change(old)
    if new == old:
        return True
    try:
        parent = open_parent(root, relative, create=True)
    except OSError:
        return False
    temporary = f".{PurePosixPath(relative).name}.sandbox-ai-{os.getpid()}"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
        with open(fd, "w", encoding="utf-8") as stream:
            stream.write(new)
        os.replace(temporary, PurePosixPath(relative).name, src_dir_fd=parent, dst_dir_fd=parent)
        return True
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary, dir_fd=parent)
        return False
    finally:
        os.close(parent)


def git_dir(workspace):
    """True if WORKSPACE/.git is a real directory (not a link, not a worktree's file)."""
    try:
        return stat.S_ISDIR(os.lstat(Path(workspace) / ".git").st_mode)
    except OSError:
        return False


def exclude_block(run, relative):
    return f"{EXCLUDE_NOTE.format(run=run)}\n/{relative}\n"


def add_exclude(workspace, run, relative):
    """Keep the canary RELATIVE out of Git: two lines in WORKSPACE/.git/info/exclude; False without a .git
    directory or when the file cannot be written."""
    if not git_dir(workspace):
        return False
    block = exclude_block(run, relative)
    return rewrite(workspace, ".git/info/exclude",
                   lambda old: old if block in old else old + ("" if not old or old.endswith("\n") else "\n") + block)


def drop_exclude(workspace, run, relative):
    if not git_dir(workspace):
        return False
    block = exclude_block(run, relative)
    return rewrite(workspace, ".git/info/exclude", lambda old: old.replace(block, "", 1))


# --- state ----------------------------------------------------------------------------------------

def state_root():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sandbox-ai" / "canaries"


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def write_json(path, document):
    private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with open(fd, "w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=1)
    os.replace(temporary, path)


def read_json(path):
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def home_state(home):
    return state_root() / "homes" / f"{hashlib.sha256(os.fsencode(str(home))).hexdigest()[:16]}.json"


def run_state(run):
    if not RUN_NAME.fullmatch(str(run)):
        raise ValueError(f"not a run name: {run!r}")
    return state_root() / "runs" / f"{run}.json"


_procinfo = None
LOADING = threading.RLock()


def process_start(pid):
    """PID's start time (procinfo.py: /proc on Linux, sysctl on macOS), or None."""
    global _procinfo
    if _procinfo is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_procinfo_for_canary", HERE / "procinfo.py")
        with LOADING:  # published only once loaded: another thread never sees a half-made module
            if _procinfo is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _procinfo = loaded
    try:
        return _procinfo.start_time(int(pid))
    except (ValueError, TypeError):
        return None


def launcher_alive(record):
    pid = record.get("pid")
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, ValueError, TypeError):
        return False
    start = record.get("pid_start")
    return start is None or process_start(pid) == start


def append_line(path, record, limit=EVENTS_LIMIT):
    try:
        if path.exists() and path.stat().st_size > limit:
            return False
        private_dir(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    except OSError:
        return False
    try:
        os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode())
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def tidy(now=None):
    """Clean up after runs that ended without doing so themselves (their launcher was killed): their workspace
    canaries and Git exclusions go; and forget runs that ended more than STATE_DAYS ago."""
    now = time.time() if now is None else now
    directory = state_root() / "runs"
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return []
    notes = []
    for path in paths:
        record = read_json(path)
        if record is None:
            continue
        if record.get("status") == "active" and not launcher_alive(record):
            for entry in record.get("canaries", []):
                if entry.get("root") == "workspace" and entry.get("planted"):
                    # The workspace is the user's: only what still is the canary goes (never a .env changed in
                    # place, nor a new one that got the old inode number).
                    state = remove_file(entry["root_path"], entry["path"], entry)
                    drop_exclude(entry["root_path"], record.get("run"), entry["path"])
                    notes.append(f"canaries: removed {entry['host_path']} left by run {record.get('run')} ({state})")
            record["status"] = "abandoned"
            with contextlib.suppress(OSError):
                write_json(path, record)
        elif record.get("status") != "active":
            with contextlib.suppress(OSError):
                if now - path.stat().st_mtime > STATE_DAYS * 86400:
                    path.unlink()
                    path.with_suffix(".events.jsonl").unlink(missing_ok=True)
    return notes


# --- one run ---------------------------------------------------------------------------------------

def summary(event, entry):
    """One sentence about EVENT of the canary ENTRY."""
    path, what = entry.get("sandbox_path", "?"), entry.get("describe", "a decoy")
    kind = event.get("event")
    target = event.get("url") or event.get("host") or event.get("destination") or "?"
    if kind == "read":
        return f"something in the sandbox opened the decoy file {path} ({what})"
    if kind == "context":
        return f"a token of the decoy file {path} went to the model provider: the agent read it into its conversation"
    if kind == "send":
        how = {"dns": "in a DNS lookup of", "push": "in a push to", "sni": "in the TLS name of"}.get(
            event.get("source") if event.get("source") != "proxy" else event.get("where"), "in a request to")
        return f"a token of the decoy file {path} left the sandbox {how} {target} " \
               f"({event.get('where') or event.get('source')}, {event.get('encoding', '?')})"
    verb = {"modify": "changed", "delete": "deleted", "move": "renamed or moved", "replace": "replaced"}.get(kind, kind)
    return f"the decoy file {path} was {verb}"


class Plan:
    """The canaries of one run: planted before it starts, watched while it runs, removed (or kept with a kept
    HOME) when it ends."""

    def __init__(self, a, home, launcher=None, notify=None, question=None, kill=os.kill, runner=subprocess.run,
                 interval=0.5):
        self.a, self.home, self.launcher = a, Path(home), launcher
        self.run = str(getattr(a, "run_id", None) or f"run-{os.getpid()}")
        self.categories = tuple(getattr(a, "canary_categories", None) or ())
        rulesets = launcher.load_rulesets(a) if launcher is not None and hasattr(launcher, "load_rulesets") else \
            getattr(a, "rulesets", None) or []
        mode = getattr(a, "mode_canaries", None)  # the security mode weighs in like a ruleset (modes.py)
        if mode and (mode.get("on_read") or mode.get("on_send")):
            rulesets = [*rulesets, {"name": f"mode {getattr(a, 'mode', '?')}",
                                    "canary": {k: mode[k] for k in ("on_read", "on_send") if k in mode}}]
        self.decisions = decisions(rulesets)
        self.kept = bool(getattr(a, "state_dir", None))
        self.entries, self.notes = [], []
        self.notify, self.question_with, self.kill, self.runner, self.interval = notify, question, kill, runner, interval
        self.control_dir = self.log_dir = None
        self.thread, self.stopping, self.lock = None, threading.Event(), threading.Lock()
        self.offset, self.last_notice, self.counter = 0, {}, 0
        self.paused = self.stopped = False
        self.question = self.started = None
        self.finished = False

    # -- planting --

    def covered(self, sandbox_path):
        """A mount on top of SANDBOX_PATH or one of its directories (--mount, kits): the file would be hidden."""
        targets = []
        for value in getattr(self.a, "mount", []) or []:
            with contextlib.suppress(ValueError, AttributeError, IndexError):
                targets.append(self.launcher.parse_mount(value)[1] if self.launcher is not None else
                               value.rsplit(":", 2)[1])
        targets += [str(dest) for _, dest, _ in getattr(self.a, "kit_mounts", []) or []]
        path = PurePosixPath(sandbox_path)
        return any(PurePosixPath(target) == path or PurePosixPath(target) in path.parents for target in targets)

    def plant_one(self, spec, root, root_path, sandbox_root, content=None):
        """Plant SPEC under ROOT_PATH; the entry, or None (with a note) when its place is taken."""
        sandbox_path = str(PurePosixPath(sandbox_root) / spec["path"])
        host_path = str(Path(root_path) / spec["path"])
        if self.covered(sandbox_path):
            self.notes.append(f"canaries: {sandbox_path} not planted: a mount lies on top of it")
            return None
        if exists(root_path, spec["path"]):
            return None  # never over what is there (yours, or the agent's)
        try:
            made = create_file(root_path, spec["path"], content if content is not None else spec["content"])
        except FileExistsError:
            return None
        except OSError as exc:
            self.notes.append(f"canaries: {host_path} not planted: {exc.strerror or exc}")
            return None
        entry = {key: spec[key] for key in ("id", "path", "reads", "describe", "tokens")}
        entry.update(category=spec["category"], root=root, root_path=str(root_path), sandbox_path=sandbox_path,
                     host_path=host_path, planted=True, content=content if content is not None else spec["content"],
                     **made)
        return entry

    def plant(self):
        """Plant the canaries of the run's categories; the notes for the user."""
        self.notes += tidy()
        home_categories = [c for c in ("credentials", "home") if c in self.categories]
        if self.kept:
            self.reconcile_home(home_categories)
        else:
            self.plant_home(home_categories, set())
        if "workspace" in self.categories:
            if getattr(self.a, "workspace_ro", False):
                self.notes.append("canaries: no workspace canaries with --workspace-ro")
            else:
                targets = self.launcher.workspace_targets(self.a) if self.launcher is not None else \
                    [(w, "/work" if i == 0 else f"/workspaces/{Path(w).name}") for i, w in enumerate(self.a.workspaces)]
                for number, (source, dest) in enumerate(targets):
                    spec = {**workspace_file(number), "category": "workspace"}
                    entry = self.plant_one(spec, "workspace", Path(source).expanduser().resolve(), dest)
                    if entry is None:
                        continue
                    entry["excluded"] = add_exclude(entry["root_path"], self.run, entry["path"])
                    self.entries.append(entry)
                if getattr(self.a, "agent", None) == "opencode" and any(e["root"] == "workspace" for e in self.entries):
                    self.notes.append("canaries: OpenCode (Bun) reads .env files at its start: expect a read of "
                                      "the workspace canary right away")
        self.save()
        return self.notes

    def plant_home(self, wanted, have):
        catalogue = {"credentials": credential_files, "home": home_files}
        for category in wanted:
            for spec in catalogue[category]():
                if spec["id"] in have:
                    continue
                entry = self.plant_one({**spec, "category": category}, "home", self.home, "/root")
                if entry is not None:
                    self.entries.append(entry)

    def reconcile_home(self, wanted):
        """A kept HOME keeps its canaries from run to run (the same files and tokens): one that is gone is planted
        again, one that was changed is no canary any more (left alone), one of a category no longer chosen is
        removed. The record is kept outside the HOME."""
        path = home_state(self.home)
        previous = (read_json(path) or {}).get("canaries", [])
        have = set()
        for entry in previous:
            if not isinstance(entry, dict) or not {"id", "path", "sha256", "category"} <= set(entry):
                continue
            entry = {**entry, "root": "home", "root_path": str(self.home), "host_path": str(self.home / entry["path"]),
                     "sandbox_path": str(PurePosixPath("/root") / entry["path"])}
            if entry["category"] not in wanted:
                state = remove_file(self.home, entry["path"], entry)
                self.notes.append(f"canaries: {entry['host_path']}: " + (
                    "removed (its category is off)" if state == "removed" else
                    "changed since it was planted; left as it is" if state == "changed" else "already gone"))
                continue
            found = read_file(self.home, entry["path"])
            if found is not None and hashlib.sha256(found[0]).hexdigest() == entry["sha256"]:
                entry.update(planted=False, dev=found[1].st_dev, ino=found[1].st_ino)
                self.entries.append(entry)
                have.add(entry["id"])
            elif found is None and not exists(self.home, entry["path"]) and entry.get("content"):
                again = self.plant_one(entry, "home", self.home, "/root")  # the same file and tokens again
                if again is not None:
                    self.entries.append(again)
                    have.add(entry["id"])
            else:
                self.notes.append(f"canaries: {entry['host_path']} changed since it was planted; it is no canary any "
                                  "more (left as it is: delete it if you do not want it)")
                have.add(entry["id"])  # its place stays taken: nothing new of the kind
        self.plant_home(wanted, have)
        self.save_home()

    def save_home(self):
        if not self.kept:
            return
        keep = [{key: entry.get(key) for key in ("id", "category", "path", "reads", "describe", "tokens", "sha256",
                                                 "created", "content")}
                for entry in self.entries if entry["root"] == "home"]
        try:
            write_json(home_state(self.home), {"home": str(self.home), "canaries": keep})
        except OSError as exc:
            self.notes.append(f"canaries: the record of the kept HOME could not be saved: {exc}")

    def save(self, status="active"):
        record = {"run": self.run, "status": status, "pid": os.getpid(), "pid_start": process_start(os.getpid()),
                  "started": self.started or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "agent": getattr(self.a, "agent", None), "workspace": (getattr(self.a, "workspaces", None) or [None])[0],
                  "home": str(self.home), "kept_home": self.kept, "categories": list(self.categories),
                  "decisions": self.decisions, "log_dir": str(self.log_dir) if self.log_dir else None,
                  "model_hosts": self.known_model_hosts(),
                  "canaries": [{key: value for key, value in entry.items() if key != "content"}
                               for entry in self.entries]}
        self.started = record["started"]
        if status != "active":
            record["ended"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            write_json(run_state(self.run), record)
        except (OSError, ValueError) as exc:
            self.notes.append(f"canaries: the run's record could not be saved: {exc}")

    # -- what the sandbox gets (never where the agent can read it) --

    def by_id(self):
        return {entry["id"]: entry for entry in self.entries}

    def tokens(self):
        return [(entry["id"], token) for entry in self.entries for token in entry.get("tokens", {}).values()
                if isinstance(token, str)]

    def proxy_entry(self):
        """proxy.json's "canary": the forms of every token, for requests and for host names; whether to block."""
        request, names = {}, {}
        for ident, token in self.tokens():
            for text, encoding in forms(token).items():
                request.setdefault(text, [text, ident, encoding])
            for text, encoding in name_forms(token).items():
                names.setdefault(text, [text, ident, encoding])
        return {"forms": list(request.values()), "names": list(names.values()),
                "block": self.decisions["on_send"] != "report", "model_hosts": self.model_hosts()}

    def model_hosts(self):
        """The hosts of the run's model providers (the lead's, the companions', --frontier, --llm)."""
        models = []
        if self.launcher is not None:
            # Every agent of the run talks to its model: the lead's provider and those of the agents --with.
            agents = [getattr(self.a, "agent", ""), *(getattr(self.launcher, "companions", lambda a: [])(self.a))]
            for agent in agents:
                models += list(getattr(self.launcher, "PROVIDERS", {}).get(agent, []))
            if getattr(self.a, "frontier", False):
                models += list(getattr(self.launcher, "FRONTIER", []))
            with contextlib.suppress(Exception):
                if getattr(self.a, "llm", None):
                    models += list(self.launcher.load_llm_module().pinned_names(self.a))
        return sorted(set(models))

    def known_model_hosts(self):
        try:
            return self.model_hosts()
        except Exception:  # noqa: BLE001 - the record is saved either way; canaries log falls back on known hosts
            return []

    def push_entry(self):
        """push.json's "canaries" (for the push check, privacy.py)."""
        grouped = {}
        for ident, token in self.tokens():
            grouped.setdefault(ident, []).append(token)
        return [{"id": ident, "tokens": tokens} for ident, tokens in grouped.items()]

    def supervisor_config(self):
        """CONTROL_DIR/canary.json: the files to watch (their paths in the sandbox) and the name forms for the
        DNS stub."""
        names = {}
        for ident, token in self.tokens():
            for text, encoding in name_forms(token).items():
                names.setdefault(text, [text, ident, encoding])
        return {"files": [{"id": entry["id"], "path": entry["sandbox_path"], "reads": entry["reads"]}
                          for entry in self.entries], "names": list(names.values())}

    def plan(self):
        """What --dry-run shows."""
        return {"categories": list(self.categories), "on_read": self.decisions["on_read"],
                "on_send": self.decisions["on_send"], "decided_by": self.decisions["by"]}

    # -- while the run lasts --

    def arm(self, control_dir, log_dir=None):
        """Hand the supervisor its list (CONTROL_DIR/canary.json) and follow what it reports."""
        self.control_dir = Path(control_dir)
        self.log_dir = Path(log_dir).expanduser() if log_dir else None
        if not self.entries:
            return self
        write_json(self.control_dir / CONFIG_NAME, self.supervisor_config())
        self.save()
        self.thread = threading.Thread(target=self.follow, name="sandbox-ai-canaries", daemon=True)
        self.thread.start()
        return self

    def follow(self):
        while not self.stopping.wait(self.interval):
            with contextlib.suppress(Exception):  # the watch must never take the sandbox down
                self.poll()

    def poll(self):
        """Handle the events reported since the last poll, a resume request and the answer to a question."""
        with self.lock:
            if self.control_dir is None:
                return
            path = self.control_dir / EVENTS_NAME
            try:
                with open(path, "rb") as stream:
                    stream.seek(self.offset)
                    data = stream.read(1 << 20)
            except OSError:
                data = b""
            end = data.rfind(b"\n") + 1
            self.offset += end
            for line in data[:end].splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self.handle(event)
            resume = self.control_dir / RESUME_NAME
            if resume.exists():
                resume.unlink(missing_ok=True)
                if self.question is not None:
                    self.close_question({"decision": "allow", "reason": "resumed with sandbox-ai canaries resume"})
                self.resume()
            if self.question is not None:
                self.check_question()

    def classify(self, event, entry):
        """The decision for EVENT: on_send for a token that left, on_read for a read (of a file whose reads
        alert) and for a token in the agent's conversation, report for a change, log for the rest."""
        kind = event.get("event")
        if kind == "send":
            return self.decisions["on_send"]
        if kind == "context" or (kind == "read" and entry.get("reads") == "alert"):
            return self.decisions["on_read"]
        if kind in ("modify", "delete", "move", "replace"):
            return "report"
        return "log"

    def handle(self, event):
        entry = self.by_id().get(str(event.get("canary")))
        if event.get("event") == "error" or entry is None:
            self.record({**event, "action": "log"})
            return
        action = self.classify(event, entry)
        record = {**{k: v for k, v in event.items() if k not in ("canary",)}, "canary": entry["id"],
                  "category": entry["category"], "path": entry["sandbox_path"], "host_path": entry["host_path"],
                  "action": action, "summary": summary(event, entry)}
        self.record(record)
        if action == "log":
            return
        if action in ("ask", "pause", "stop") or self.due(entry["id"], event.get("event")):
            self.act(action, record)

    def due(self, ident, kind):
        now = time.monotonic()
        if now - self.last_notice.get((ident, kind), -NOTIFY_REPEAT) < NOTIFY_REPEAT:
            return False
        self.last_notice[(ident, kind)] = now
        return True

    def record(self, record):
        record = {"time": record.get("time") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "run": self.run,
                  **{k: v for k, v in record.items() if k != "time"}}
        if self.log_dir is not None:
            append_line(self.log_dir / LOG_NAME, record)
        with contextlib.suppress(ValueError):
            append_line(run_state(self.run).with_suffix(".events.jsonl"), record)

    def act(self, action, record):
        if self.stopped:
            return
        if action == "ask":
            self.ask(record)
            return
        if action == "pause":
            paused = self.pause()
            self.notice(record, " The sandbox is paused" + (f": sandbox-ai canaries resume {self.run}" if paused
                                                            else " (it could not be paused)"), action)
            return
        if action == "stop":
            self.notice(record, " The sandbox is stopped.", action)
            self.stop()
            return
        self.notice(record, " The request was refused." if action == "block" else "", action)

    def approvals(self):
        spec = importlib.util.spec_from_file_location("sandbox_approvals_for_canaries", HERE / "approvals.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def title(self):
        with contextlib.suppress(Exception):
            origin = self.approvals().origin(self.approvals().run_record(self.control_dir / "approvals"))
            if origin:
                return f"sandbox-ai canary: {origin}"
        return f"sandbox-ai canary: {self.run}"

    def write_record(self, document, done=None):
        """A record in the run's approvals directory (sandbox-ai approvals history, the app, the timeline)."""
        directory = private_dir(self.control_dir / "approvals")
        self.counter += 1
        ident = f"{int(time.time())}-{secrets.randbelow(10 ** 9)}-{self.counter}"
        payload = {"id": ident, "kind": "canary", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "sandbox": {"run": self.run}, **document}
        (directory / f"{ident}.json").write_text(json.dumps(payload))
        if done is not None:
            (directory / f"{ident}.done").write_text(json.dumps({"id": ident, **done}))
        return ident

    def notice(self, record, suffix, action):
        text = record["summary"][0].upper() + record["summary"][1:] + "." + suffix
        with contextlib.suppress(OSError):
            self.write_record({k: record.get(k) for k in ("canary", "path", "event", "source", "where", "url", "host",
                                                          "encoding", "summary", "action")},
                              {"decision": "deny" if action in ("block", "stop") else "noted", "scope": "run",
                               "reason": f"canary: {action}"})
        notify = self.notify
        if notify is None:
            notify = self.approvals().desktop_notification
        with contextlib.suppress(Exception):
            notify(self.title(), text)

    def ask(self, record):
        if self.question is not None:
            return
        paused = self.pause()
        state = "The sandbox is paused until you answer" if paused else "The sandbox could not be paused"
        text = record["summary"][0].upper() + record["summary"][1:] + f". {state}: continue, or stop it?"
        question = {k: record.get(k) for k in ("canary", "path", "event", "source", "where", "url", "host",
                                               "encoding", "summary")}
        ident = self.write_record({**question, "timeout": ASK_SECONDS, "paused": paused, "summary": text})
        self.question = (ident, time.monotonic() + ASK_SECONDS)
        asker = self.question_with
        if asker is None:
            approvals = self.approvals()
            if approvals.notify_actions():
                def asker(title, body, ident):
                    approvals.desktop_question(title, body, ident, actions=CANARY_ACTIONS, seconds=ASK_SECONDS)
            else:
                def asker(title, body, ident):
                    approvals.desktop_notification(title, f"{body}\nsandbox-ai approvals allow {ident} (continue) "
                                                          f"or deny {ident} (stop)")
        def put():
            with contextlib.suppress(Exception):
                asker(self.title(), text, ident)
        threading.Thread(target=put, daemon=True).start()

    def check_question(self):
        ident, deadline = self.question
        directory = self.control_dir / "approvals"
        answer = read_json(directory / f"{ident}.answer")
        if answer is not None:
            if answer.get("decision") == "allow":
                self.close_question({"decision": "allow", "scope": "once", "reason": "answered: continue"})
                self.resume()
            else:
                self.close_question({"decision": "deny", "scope": "run", "reason": "answered: stop"})
                self.stop()
        elif time.monotonic() > deadline:
            self.close_question({"decision": "deny", "scope": "once", "reason": f"no answer within {ASK_SECONDS} s: "
                                 f"the sandbox stays paused (sandbox-ai canaries resume {self.run}, or "
                                 f"sandbox-ai net stop {self.run})"})

    def close_question(self, result):
        ident, _ = self.question
        self.question = None
        with contextlib.suppress(OSError):
            (self.control_dir / "approvals" / f"{ident}.done").write_text(json.dumps({"id": ident, **result}))
        self.record({"event": "answer", "source": "you", "canary": None, "action": result["decision"],
                     "summary": result["reason"]})

    def marker(self):
        """Where sandbox-ai list and resume see that this sandbox is paused: a session's paused file (in its start
        directory, which holds the tmux socket directory; its pod is agent-session-NAME before 0.1.30, a pod per
        run since), a kept task's in its start directory, else the run's own (sandbox-ai pause of a task)."""
        a = self.a
        socket_dir, pod = getattr(a, "tmux_socket_dir", None), getattr(a, "pod_name", None) or ""
        if socket_dir and (pod.startswith("agent-session-") or getattr(a, "start_id", None)):
            return Path(socket_dir).expanduser().parent / "paused"
        if getattr(a, "start_key", None):
            try:
                return self.launcher.load_sessions_module().session_dir(a.start_key) / "paused"
            except (ValueError, AttributeError):
                return None
        control = getattr(a, "control_dir", None)
        return Path(control) / "paused" if control else None

    def freeze(self, thaw=False):
        """Freeze (or THAW) the sandbox: its Podman pod, or every process of the Bubblewrap sandbox."""
        a = self.a
        pod, pid = getattr(a, "running_pod", None), getattr(a, "running_pid", None)
        if a.backend == "podman" and pod:
            self.runner(self.launcher.podman_command(a, "pod", "unpause" if thaw else "pause", pod), check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=60)
            return True
        if pid:
            self.launcher.load_sessions_module().signal_tree(pid, signal.SIGCONT if thaw else signal.SIGSTOP)
            return True
        return False

    def pause(self):
        if self.paused:
            return True
        try:
            if not self.freeze():
                return False
        except (OSError, subprocess.SubprocessError, AttributeError) as exc:
            self.record({"event": "error", "error": f"could not pause: {exc}", "action": "log"})
            return False
        self.paused = True
        marker = self.marker()
        if marker is not None:
            with contextlib.suppress(OSError):
                marker.touch()
        self.record({"event": "paused", "canary": None, "action": "pause",
                     "summary": f"paused; continue with sandbox-ai canaries resume {self.run}"})
        return True

    def resume(self):
        if not self.paused:
            return False
        marker = self.marker()
        if marker is not None and not marker.exists():
            self.paused = False  # thawed meanwhile by sandbox-ai session resume
            return True
        try:
            self.freeze(thaw=True)
        except (OSError, subprocess.SubprocessError, AttributeError) as exc:
            self.record({"event": "error", "error": f"could not resume: {exc}", "action": "log"})
            return False
        self.paused = False
        if marker is not None:
            marker.unlink(missing_ok=True)
        if getattr(self, "control_dir", None):  # the kill switch asked for while it was paused (sandbox-ai net cut)
            with contextlib.suppress(Exception):
                self.launcher.load_starts_module().module("net").apply_pending_cut(self.control_dir)
        self.record({"event": "resumed", "canary": None, "action": "resume", "summary": "resumed"})
        return True

    def stop(self):
        """End the run as sandbox-ai net stop does: SIGTERM to this launcher, which cleans up."""
        if self.stopped:
            return
        self.stopped = True
        if self.paused:
            self.resume()
        self.record({"event": "stopped", "canary": None, "action": "stop", "summary": "the sandbox was stopped"})
        self.kill(os.getpid(), signal.SIGTERM)

    # -- the end --

    def drain(self):
        """Stop following and handle what the sandbox reported last (before its control directory goes)."""
        if self.stopping.is_set():
            return
        self.stopping.set()
        if self.thread is not None:
            self.thread.join(5)
        with contextlib.suppress(Exception):
            self.poll()

    def finish(self):
        """After the run: read what is left, remove the canaries (a kept HOME keeps its own), forget the
        exclusions; the notes for the user."""
        if self.finished:
            return []
        self.finished = True
        self.drain()
        notes, made = [], {}
        for entry in self.entries:
            if entry["root"] == "home" and self.kept:
                continue
            # In the sandbox's HOME the same file goes even when changed (the run's own); in a workspace only what
            # still is the canary: the user's edits in place (a .env) are never deleted.
            state = remove_file(entry["root_path"], entry["path"], entry, force_same_inode=entry["root"] != "workspace",
                                dirs=False)
            if state != "changed":
                made.setdefault(entry["root_path"], []).extend(entry.get("created", []))
            if entry["root"] == "workspace":
                drop_exclude(entry["root_path"], self.run, entry["path"])
                if state == "changed":
                    notes.append(f"canaries: {entry['host_path']} was replaced during the run; left as it is "
                                 "(check it: it may hold a canary token)")
            entry["state"] = state
        for root, created in made.items():  # after every file: canaries share directories (~/.config)
            remove_dirs(root, created)
        self.save("ended")
        events = self.events()
        alerts = [event for event in events if event.get("action") not in ("log", None) and event.get("canary")]
        if alerts:
            notes.append(f"canaries: {len(alerts)} event(s) in this run, e.g. {alerts[0].get('summary')}; "
                         f"see sandbox-ai canaries list {self.run}")
        return notes

    def events(self):
        return read_events(self.run)


CANARY_ACTIONS = (("continue", "allow", "once", "Continue"), ("stop", "deny", "run", "Stop the sandbox"))


def read_events(run, limit=EVENTS_LIMIT):
    try:
        with open(run_state(run).with_suffix(".events.jsonl"), "rb") as stream:
            lines = stream.read(limit).splitlines()
    except (OSError, ValueError):
        return []
    events = []
    for line in lines:
        with contextlib.suppress(ValueError):
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
    return events


def plant(a, home, launcher=None, **options):
    """The canaries of a launch (agent_sandbox.launch), planted; None when the run has none and nothing of an
    earlier one is left to tidy."""
    plan = Plan(a, home, launcher, **options)
    if not plan.categories and not (plan.kept and home_state(plan.home).exists()):
        plan.notes += tidy()
        return plan if plan.notes else None
    plan.plant()
    return plan


# --- command line --------------------------------------------------------------------------------

def shown(value, limit=300):
    text = str(value)[:limit]
    return "".join(c if " " <= c <= "~" else c.encode("unicode_escape").decode("ascii") for c in text)


def textout():
    spec = importlib.util.spec_from_file_location("sandbox_ai_textout_for_canaries", HERE / "textout.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def masked(token):
    return token[:6] + "…" if len(token) > 10 else "…"


def list_runs(out=print, as_json=False):
    runs = []
    for path in sorted((state_root() / "runs").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        record = read_json(path)
        if record is None:
            continue
        status = record.get("status")
        if status == "active" and not launcher_alive(record):
            status = "abandoned"
        events = read_events(record.get("run", path.stem))
        runs.append({"run": record.get("run", path.stem), "started": record.get("started"), "status": status,
                     "canaries": len(record.get("canaries", [])), "events": len(events),
                     "alerts": sum(1 for e in events if e.get("action") not in ("log", None) and e.get("canary"))})
    if as_json:
        out(json.dumps(runs, indent=2))
        return 0
    textout().table(["RUN", "STARTED", "STATUS", "CANARIES", "EVENTS", "ALERTS"],
                    [[shown(r["run"]), shown(r["started"]), shown(r["status"]), str(r["canaries"]), str(r["events"]),
                      str(r["alerts"])] for r in runs], out, indent=0, empty="no runs with canaries")
    return 0


def show_run(run, out=print, tokens=False, as_json=False):
    record = read_json(run_state(run))
    if record is None:
        raise ValueError(f"no canaries recorded for run {shown(run)}; see sandbox-ai canaries list")
    events = read_events(run)
    canaries = []
    for entry in record.get("canaries", []):
        item = {key: entry.get(key) for key in ("id", "category", "sandbox_path", "host_path", "describe", "reads",
                                                 "state", "excluded")}
        item["tokens"] = entry.get("tokens", {}) if tokens else {k: masked(v) for k, v in entry.get("tokens", {}).items()}
        canaries.append(item)
    if as_json:
        out(json.dumps({key: record.get(key) for key in ("run", "status", "started", "ended", "home", "kept_home",
                                                         "categories", "decisions")} | {"canaries": canaries,
                                                                                         "events": events}, indent=2))
        return 0
    t = textout()
    out(f"run {shown(record.get('run'))} ({shown(record.get('status'))}, started {shown(record.get('started'))}); "
        f"on a read: {shown(record.get('decisions', {}).get('on_read'))}, when a token leaves: "
        f"{shown(record.get('decisions', {}).get('on_send'))}")
    t.table(["ID", "CATEGORY", "IN THE SANDBOX", "ON THE HOST", "STATE", "TOKENS"],
            [[shown(c["id"]), shown(c["category"]), shown(c["sandbox_path"]), shown(c["host_path"], 500),
              shown(c.get("state") or ("planted" if record.get("status") == "active" else "?")),
              ", ".join(f"{shown(k)}={shown(v, 200)}" for k, v in c["tokens"].items())] for c in canaries],
            out, indent=0, empty="no canaries were planted")
    out("")
    t.table(["TIME", "CANARY", "EVENT", "ACTION", "WHAT"],
            [[shown(e.get("time")), shown(e.get("canary") or "-"), shown(e.get("event")), shown(e.get("action")),
              shown(e.get("summary") or e.get("error") or "", 500)] for e in events],
            out, indent=0, empty="nothing happened to them")
    return 0


# --- the canary log: every event, who, and what the agent did around it ------------------------------
#
# `sandbox-ai canaries log` puts each event of a run next to the run's other records: the process the
# supervisor found holding the file (file events; inotify itself does not say who) or that sent the request
# (the proxy, the DNS stub), the agent's tool calls in the same time window (activity.jsonl: the one that names
# the file first), the prompt turn it belongs to, and the requests to the model provider just before and after
# (the proxy's connections-*.jsonl), each with the id of its activity record where the timeline has one.
# Everything shown is redacted as the activity log redacts, and the run's canary tokens are masked.

TOOL_BEFORE = 30.0           # seconds before an event a tool call may have started and still be the cause
TOOL_AFTER = 5.0             # seconds after (the clocks of the transcript and of the supervisor differ a little)
REQUEST_WINDOW = 120.0       # seconds around an event in which model requests are linked
REQUESTS_AROUND = 2          # model requests linked before and after an event
CONNECTIONS_READ = 256 << 20  # bytes of connection logs read per run
DEFAULT_MODEL_HOSTS = ("api.anthropic.com", "console.anthropic.com", "claude.ai", "api.openai.com", "chatgpt.com",
                       "opencode.ai")  # for runs recorded before the record named its providers
PROCESS_KEYS = ("pid", "ns_pid", "comm", "argv", "exe", "cwd", "uid", "parents", "fd")

_modules = {}


def module(name):
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_canaries_log", HERE / f"{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _modules[name] = loaded
    return _modules[name]


def process_of(event):
    """The process an event names, as {pid, comm, argv, ...} or {"unknown": why}."""
    process = event.get("process")
    if isinstance(process, dict) and not process.get("unknown"):
        return {key: process[key] for key in PROCESS_KEYS if key in process}
    if isinstance(process, dict):
        return {"unknown": str(process.get("unknown"))}
    if isinstance(process, str) and process:
        return {"comm": process}
    if event.get("source") == "files":
        return {"unknown": "inotify does not say who (recorded before the supervisor looked it up)"}
    if event.get("source") == "push":
        return {"unknown": "found by the push check"}
    return {"unknown": "not recorded"}


def process_text(process):
    if not isinstance(process, dict) or process.get("unknown") is not None or not process:
        why = process.get("unknown") if isinstance(process, dict) else None
        return f"unknown ({why})" if why else "unknown"
    argv = process.get("argv") if isinstance(process.get("argv"), list) else []
    text = f"{process.get('comm') or '?'} (pid {process.get('pid', '?')}"
    if process.get("ns_pid") not in (None, process.get("pid")):
        text += f", {process['ns_pid']} in the sandbox"
    text += ")"
    if argv:
        text += ": " + " ".join(str(a) for a in argv)
    if process.get("parents"):
        text += " <- " + " <- ".join(str(p) for p in process["parents"][:3])
    return text


def activity_records(log_dir, run):
    """The run's activity records (the last version of each id), in time order."""
    activity = module("activity")
    if not log_dir:
        return []
    records = {}
    for record in activity.read_records(Path(log_dir) / activity.ACTIVITY_FILE, keep=lambda r: r.get("run") == run):
        if isinstance(record.get("id"), str):
            records[record["id"]] = record
    return sorted(records.values(), key=lambda r: activity.epoch(r.get("time")) or 0)


def request_host(record):
    host = record.get("host")
    if not isinstance(host, str) or not host:
        match = re.match(r"[a-z][a-z0-9+.-]*://([^/?#:]+)", str(record.get("url") or ""))
        host = match.group(1) if match else ""
    return host.lower().rstrip(".").split(":")[0]


def model_requests(log_dir, run, hosts):
    """[request] of the proxy's connection logs of RUN that went to HOSTS (or carried a canary token), with the id
    of the activity record each one became (if the timeline has one: repeated requests are only counted there)."""
    activity = module("activity")
    if not log_dir:
        return []
    hosts = {h.lower() for h in hosts}
    needles = [h.encode() for h in hosts] + [b'"canary"']
    found, budget = [], CONNECTIONS_READ
    try:
        paths = sorted(Path(log_dir).glob(f"connections-*-{run}.jsonl"))
    except OSError:
        return []
    for path in paths:
        if path.is_symlink() or not path.is_file() or budget <= 0:
            continue
        try:
            with open(path, "rb") as stream:
                offset = 0
                for line in stream:
                    start, offset = offset, offset + len(line)
                    budget -= len(line)
                    if budget <= 0:
                        break
                    if not line.endswith(b"\n") or not any(n in line for n in needles):
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    host = request_host(record)
                    if host not in hosts and not isinstance(record.get("canary"), dict):
                        continue
                    seconds = activity.epoch(record.get("time"))
                    if seconds is None:
                        continue
                    ident = hashlib.sha256(f"{run}\0proxy\0proxy:{start}".encode("utf-8", "replace")).hexdigest()[:16]
                    found.append({"time": record.get("time"), "epoch": seconds, "host": host,
                                  "event": record.get("event"), "method": record.get("method"),
                                  "url": record.get("url"), "status": record.get("status"),
                                  "model": host in hosts, "activity_id": ident,
                                  "canary": record["canary"] if isinstance(record.get("canary"), dict) else None,
                                  "log": path.name, "offset": start})
        except OSError:
            continue
    return sorted(found, key=lambda r: r["epoch"])


def mentions(record, needles):
    text = (str(record.get("summary") or "") + " " + json.dumps(record.get("details") or {}, ensure_ascii=False))
    return any(n and n in text for n in needles)


def tool_entry(record, match):
    details = record.get("details") if isinstance(record.get("details"), dict) else {}
    return {"id": record.get("id"), "time": record.get("time"), "kind": record.get("kind"), "agent": record.get("agent"),
            "summary": record.get("summary"), "match": match,
            "details": {k: details[k] for k in ("tool", "name", "command", "arguments", "status", "exit_code", "path",
                                                "duration", "output") if k in details}}


def correlate(event, records, requests):
    """{"tool_calls", "turn", "requests"} of EVENT: the tool calls in its time window (those that name the file
    first, then the nearest), the prompt turn before it, the model requests just before and after it."""
    activity = module("activity")
    at = activity.epoch(event.get("time"))
    result = {"tool_calls": [], "turn": None, "requests": []}
    if at is None:
        return result
    path = str(event.get("path") or "")
    needles = [n for n in {path, os.path.basename(path), path.replace("/root/", "~/", 1)} if len(n) >= 3]
    timed = [(activity.epoch(r.get("time")), r) for r in records]
    window = [(t, r) for t, r in timed if t is not None and r.get("kind") in ("tool", "command", "mcp")
              and at - TOOL_BEFORE <= t <= at + TOOL_AFTER]
    named = [(t, r) for t, r in window if mentions(r, needles)]
    if named:
        chosen = sorted(named, key=lambda item: abs(at - item[0]))[:3]
        result["tool_calls"] = [tool_entry(r, "names the file") for _, r in chosen]
    elif window:
        before = [(t, r) for t, r in window if t <= at + 1]
        t, r = max(before, key=lambda item: item[0]) if before else min(window, key=lambda item: item[0])
        result["tool_calls"] = [tool_entry(r, "same time")]
    prompts = [(t, r) for t, r in timed if t is not None and r.get("kind") == "prompt" and t <= at + 1]
    if prompts:
        t, r = prompts[-1]
        result["turn"] = {"id": r.get("id"), "time": r.get("time"), "agent": r.get("agent"), "summary": r.get("summary")}
    ids = {r.get("id") for r in records}
    near = [q for q in requests if abs(q["epoch"] - at) <= REQUEST_WINDOW]
    # A request that carried this canary's token: the one the event is about (a token that left, or entered the
    # agent's conversation: the same second), or after a read, the first that took the file's content on.
    own = [q for q in near if q.get("canary") and str(q["canary"].get("canary")) == str(event.get("canary"))]
    if event.get("event") in ("send", "context"):
        carried = [q for q in own if abs(q["epoch"] - at) <= 2][:1]
    else:
        carried = [q for q in own if q["epoch"] >= at][:1]
    before = [q for q in near if q["model"] and q["epoch"] < at and q not in carried][-REQUESTS_AROUND:]
    after = [q for q in near if q["model"] and q["epoch"] >= at and q not in carried][:REQUESTS_AROUND]
    chosen = [("carried the token", q) for q in carried] + [("before", q) for q in before] + \
        [("same second" if int(q["epoch"]) == int(at) else "after", q) for q in after]
    for position, q in sorted(chosen, key=lambda item: item[1]["epoch"]):
        result["requests"].append({"position": position, "time": q["time"], "method": q["method"], "url": q["url"],
                                   "host": q["host"], "status": q["status"], "event": q["event"],
                                   "activity_id": q["activity_id"] if q["activity_id"] in ids else None,
                                   "log": q["log"], "offset": q["offset"]})
    return result


def log_entries(run):
    """The canary log of RUN: [entry] in time order (see the comment above)."""
    record = read_json(run_state(run))
    if record is None:
        raise ValueError(f"no canaries recorded for run {shown(run)}; see sandbox-ai canaries list")
    activity = module("activity")
    tokens = [token for entry in record.get("canaries", []) for token in (entry.get("tokens") or {}).values()
              if isinstance(token, str)]
    redact = activity.Redactor(tokens)
    log_dir = record.get("log_dir")
    records = activity_records(log_dir, run)
    hosts = record.get("model_hosts") or DEFAULT_MODEL_HOSTS
    requests = model_requests(log_dir, run, hosts)
    entries = []
    for event in read_events(run):
        entry = {key: event.get(key) for key in ("time", "canary", "category", "path", "host_path", "event", "source",
                                                 "where", "url", "host", "encoding", "action", "summary", "error")
                 if event.get(key) is not None}
        entry["run"] = run
        entry["process"] = process_of(event)
        if isinstance(event.get("processes"), list):
            entry["processes"] = [process_of({"process": p}) for p in event["processes"][:4]]
        if event.get("canary"):
            entry.update(correlate(event, records, requests))
        entries.append(activity.clean(entry, redact, limit=activity.TEXT_LIMIT, budget=[2000]))
    return {"run": run, "status": record.get("status"), "started": record.get("started"),
            "agent": record.get("agent"), "log_dir": log_dir, "activity": bool(records),
            "connections": bool(requests), "events": entries}


def resolve_run(run):
    if run_state(run).exists():
        return run
    with contextlib.suppress(Exception):
        other = module("activity").run_id(run)  # the name of a running sandbox or of a kept start
        if other and RUN_NAME.fullmatch(str(other)) and run_state(other).exists():
            return other
    return run


def show_log(run=None, out=print, as_json=False):
    if run:
        logs = [log_entries(resolve_run(run))]
    else:
        logs = []
        for path in sorted((state_root() / "runs").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.name.endswith(".events.jsonl") or not RUN_NAME.fullmatch(path.stem):
                continue
            if read_events(path.stem):
                with contextlib.suppress(ValueError):
                    logs.append(log_entries(path.stem))
    if as_json:
        out(json.dumps(logs if not run else logs[0], indent=2, ensure_ascii=False))
        return 0
    if not logs or not any(log["events"] for log in logs):
        out("no canary events" + (f" in run {shown(run)}" if run else ""))
        return 0
    for log in logs:
        out(f"run {shown(log['run'])} ({shown(log.get('agent') or '?')}, {shown(log.get('status'))}, started "
            f"{shown(log.get('started'))}): {len(log['events'])} event(s)")
        if not log["activity"]:
            out("  (no activity timeline for this run: tool calls and prompts cannot be linked)")
        for entry in log["events"]:
            out("")
            head = f"  {shown(entry.get('time'))}  {shown(entry.get('event', '?')).upper()}  " \
                   f"{shown(entry.get('canary') or '-')}  {shown(entry.get('path') or '')}"
            out(head + (f"  [{shown(entry.get('action'))}]" if entry.get("action") else ""))
            if entry.get("summary") or entry.get("error"):
                out(f"    {shown(entry.get('summary') or entry.get('error'), 500)}")
            out(f"    process: {shown(process_text(entry.get('process')), 600)}")
            for other in (entry.get("processes") or [])[1:]:
                out(f"    also:    {shown(process_text(other), 600)}")
            for call in entry.get("tool_calls") or []:
                out(f"    tool call ({shown(call.get('match'))}): {shown(call.get('summary'), 300)}  "
                    f"[{shown(call.get('time'))}, activity {shown(call.get('id'))}]")
            if entry.get("turn"):
                turn = entry["turn"]
                out(f"    prompt turn: {shown(turn.get('summary'), 200)}  [{shown(turn.get('time'))}, activity "
                    f"{shown(turn.get('id'))}]")
            for request in entry.get("requests") or []:
                line = f"    model request ({shown(request.get('position'))}): {shown(request.get('method') or '')} " \
                       f"{shown(request.get('url') or request.get('host') or '', 300)}"
                if request.get("status"):
                    line += f" → {shown(request['status'])}"
                line += f"  [{shown(request.get('time'))}"
                line += f", activity {shown(request['activity_id'])}]" if request.get("activity_id") else \
                    f", {shown(request.get('log'))} @{shown(request.get('offset'))}]"
                out(line)
        out("")
    return 0


def resume(run, out=print):
    spec = importlib.util.spec_from_file_location("sandbox_net_for_canaries", HERE / "net.py")
    net = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(net)
    record = net.choose(net.running(), run, command="canaries resume")  # its run id, or its name (sandbox-ai list)
    run = record.get("run", run)
    (Path(record["directory"]) / RESUME_NAME).write_text("1\n")
    out(f"asked {shown(run)} to continue")
    return 0


def main(argv, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai canaries", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:4]))
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="runs with canaries, or one run's canaries and events")
    listing.add_argument("run", nargs="?")
    listing.add_argument("--tokens", action="store_true", help="show the tokens in full")
    listing.add_argument("-j", "--json", action="store_true")
    logging = commands.add_parser("log", help="every canary event with its process, the tool call, the prompt "
                                              "and the model requests around it")
    logging.add_argument("run", nargs="?", help="a run id or the name of a start (default: every run)")
    logging.add_argument("-j", "--json", action="store_true")
    resuming = commands.add_parser("resume", help="continue a sandbox a canary paused")
    resuming.add_argument("run")
    args = parser.parse_args(argv)
    try:
        if args.command == "resume":
            return resume(args.run, out)
        if args.command == "log":
            return show_log(args.run, out, args.json)
        if args.run:
            return show_run(args.run, out, args.tokens, args.json)
        return list_runs(out, args.json)
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai canaries: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
