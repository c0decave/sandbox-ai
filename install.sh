#!/usr/bin/env bash
# sandbox-ai installer: installs or updates to the newest release.
#
# User install (default): ~/.local/share/sandbox-ai, commands linked into
# ~/.local/bin and, if it exists, ~/.bin. System install (root or --system):
# /usr/local/lib/sandbox-ai, commands in /usr/local/bin.
set -euo pipefail

# Where releases come from: a Gitea/Forgejo or GitHub repository URL.
DEFAULT_SOURCE="https://github.com/c0decave/sandbox-ai"
COMMANDS=(sandbox-ai)
# The single commands of versions before 0.1.92 (deprecated since 0.1.30; each is a command of sandbox-ai now): their
# links into this installation go at every install, update and uninstall.
OLD_COMMANDS=(claude-sandbox codex-sandbox opencode-sandbox sandbox-session sandbox-mcp sandbox-wizard sandbox-build-image)
# The graphical front end (projects/sandbox-ai-app); linked when the tree contains it.
APP_COMMAND=sandbox-ai-app
# PySide6 6.12.0 crashes below Python 3.12 (it releases None once too often).
APP_PACKAGES=("PySide6-Essentials>=6.6,!=6.12.0; python_version < '3.12'" "PySide6-Essentials>=6.6; python_version >= '3.12'")
APP_TERMINAL_PACKAGES=("PySide6-Addons>=6.6,!=6.12.0; python_version < '3.12'" "PySide6-Addons>=6.6; python_version >= '3.12'"
                       "PySide6-WebEngine>=6.12; python_version >= '3.12'")
KEEP_VERSIONS=2
# Release signatures: SHA256SUMS.sig, an SSH signature (ssh-keygen -Y) over SHA256SUMS by a key in the tree's
# sandbox/release-signers. An administrator's policy can demand them (require_signed_updates).
SIGNERS=sandbox/release-signers
POLICY_FILE=/etc/sandbox-ai/policy.json
POLICY_OWNER=0
# The release keys for an installation that has no tree yet (curl ... | bash): scripts/make_dist.py copies
# sandbox/release-signers in here when it builds the published install.sh. Trust on first use.
# BEGIN EMBEDDED RELEASE SIGNERS
EMBEDDED_SIGNERS=''
# END EMBEDDED RELEASE SIGNERS

usage() {
  cat <<EOF
Usage: install.sh [options]

Installs the newest sandbox-ai release, or updates an existing installation.

  --source URL      Repository with releases (default: \$SANDBOX_AI_SOURCE or
                    $DEFAULT_SOURCE); Gitea, Forgejo or https://github.com/OWNER/REPO
  --version X.Y.Z   Install this release instead of the newest
  --from PATH       Install from a checkout, a bundle .tar.gz or an unpacked bundle
  --user            Install for this user (default unless run as root)
  --system          Install for all users under --prefix (needs root)
  --prefix DIR      System prefix (default: /usr/local)
  --bin-dir DIR     Link the commands into DIR only
  --no-modify-rc    Leave ~/.bashrc and ~/.zshrc alone (default for a user install:
                    a marked block there puts the command directory on PATH)
  --force           Reinstall the same version; replace foreign files in bin directories
  --check           Show the installed and the newest version and its signature, change nothing
  --fetch DIR       Download the newest release (or --version) into DIR and check it fully - SHA256SUMS
                    and a signature by a trusted key - without installing it (sandbox-ai update mode
                    download; install it later with --from DIR/sandbox-ai-bundle-X.Y.Z.tar.gz --verify)
  --allow-downgrade Install a release older than the installed version
  --allow-unsigned  Install a release without a valid signature by a trusted key (not
                    recommended; refused when $POLICY_FILE sets
                    require_signed_updates)
  --verify          With --from BUNDLE.tar.gz: check it against the SHA256SUMS and
                    SHA256SUMS.sig next to it, like a download
  --uninstall       Remove the installation, its command links and the PATH block
  --dry-run         With --uninstall: list what it removes (KIND, a tab, PATH on each
                    line; sandbox-ai uninstall shows it as a table), remove nothing
  --no-color        Without colour (also NO_COLOR=1; the default: colour on a terminal)

The app (sandbox-ai-app, the graphical front end):
  --app-deps        Install PySide6 into the app's own environment (DATA/app-env)
                    even when python3 already has it
  --no-app-deps     Never download PySide6 (default: only when python3 lacks it)
  --no-app-terminal Without terminal tabs inside the app (they need QtWebEngine,
                    about 200 MB more, and xterm.js from registry.npmjs.org,
                    checked against vendor/xterm.json); commands then open in
                    terminal windows
  --no-app          Neither link sandbox-ai-app nor install anything for it
  -h, --help        This help

Signed releases: a release signs its SHA256SUMS with an SSH key (SHA256SUMS.sig).
Once the installed tree lists a key in $SIGNERS, an update must be signed by
one of those keys; a release may change the keys only with such a signature. A
first install trusts the keys that came with install.sh. Checking needs
ssh-keygen (OpenSSH 8.1 or newer). Releases older than the installed version are
refused (--allow-downgrade).

Private repositories: put an access token into SANDBOX_AI_TOKEN. It is sent
only to the repository's own host, never printed, and never over plain http
to anything but localhost.
EOF
}

# Colour on a terminal, as sandbox-ai's own messages (sandbox/textout.py): an error red, a warning yellow, what to
# run next in bold; never with NO_COLOR, TERM=dumb or --no-color, never in a pipe or a log; FORCE_COLOR forces it.
colour=0
for arg in "$@"; do
  case "$arg" in --no-color|--color=never) export NO_COLOR=1; unset FORCE_COLOR ;; --color=always) export FORCE_COLOR=1; unset NO_COLOR ;; esac
done
if [[ -z "${NO_COLOR:-}" ]] && { [[ -n "${FORCE_COLOR:-}" ]] || [[ -t 2 && "${TERM:-}" != dumb ]]; }; then colour=1; fi
say() {  # KIND TEXT: one line "install.sh: TEXT" on standard error
  local kind=$1 code=""
  shift
  case "$kind" in error) code=31 ;; warning) code=33 ;; hint) code=1 ;; esac
  if [[ $colour == 1 && -n "$code" ]]; then
    printf '\033[1minstall.sh:\033[0m \033[%sm%s\033[0m\n' "$code" "$*" >&2
  elif [[ $colour == 1 ]]; then
    printf '\033[1minstall.sh:\033[0m %s\n' "$*" >&2
  else
    printf 'install.sh: %s\n' "$*" >&2
  fi
}
die() { say error "$*"; exit 1; }
note() { case "$*" in warning:*) say warning "$*" ;; *) say note "$*" ;; esac; }

source_url=${SANDBOX_AI_SOURCE:-$DEFAULT_SOURCE}
want_version="" from="" fetch_dir="" mode="" prefix=/usr/local bin_override="" force=0 action=install dry_run=0
app=1 app_deps=auto app_terminal=1 allow_downgrade=0 allow_unsigned=0 verify=0 modify_rc=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source) [[ $# -ge 2 ]] || die "--source needs a URL"; source_url=$2; shift 2 ;;
    --version) [[ $# -ge 2 ]] || die "--version needs X.Y.Z"; want_version=${2#v}; shift 2 ;;
    --from) [[ $# -ge 2 ]] || die "--from needs a path"; from=$2; shift 2 ;;
    --user) mode=user; shift ;;
    --system) mode=system; shift ;;
    --prefix) [[ $# -ge 2 ]] || die "--prefix needs a directory"; prefix=$2; shift 2 ;;
    --bin-dir) [[ $# -ge 2 ]] || die "--bin-dir needs a directory"; bin_override=$2; shift 2 ;;
    --force) force=1; shift ;;
    --check) action=check; shift ;;
    --fetch) [[ $# -ge 2 ]] || die "--fetch needs a directory"; fetch_dir=$2; action=fetch; shift 2 ;;
    --allow-downgrade) allow_downgrade=1; shift ;;
    --allow-unsigned) allow_unsigned=1; shift ;;
    --verify) verify=1; shift ;;
    --uninstall) action=uninstall; shift ;;
    --dry-run) dry_run=1; shift ;;
    --no-modify-rc) modify_rc=0; shift ;;
    --no-color|--color=never|--color=always|--color=auto) shift ;;  # (taken above)
    --app-deps) app_deps=yes; shift ;;
    --no-app-deps) app_deps=no; shift ;;
    --app-terminal) app_terminal=1; shift ;;
    --no-app-terminal) app_terminal=0; shift ;;
    --no-app) app=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) say error "unknown option: $1"; usage >&2; exit 2 ;;
  esac
done
[[ $dry_run == 0 || $action == uninstall ]] || die "--dry-run goes with --uninstall"
[[ -z "$want_version" || "$want_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]+)?$ ]] \
  || die "--version expects X.Y.Z"

command -v python3 >/dev/null || die "python3 is required (3.9 or newer)"
python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "python3 3.9 or newer is required"

if [[ -z "$mode" ]]; then
  if [[ $(id -u) == 0 ]]; then mode=system; else mode=user; fi
fi
if [[ $mode == system ]]; then
  [[ $(id -u) == 0 || -w "$prefix" ]] || die "--system needs root: sudo bash install.sh --system"
  # For every user: what is made here is open to all and writable by its owner only, whatever root's umask (077 on
  # hardened systems or in a root shell made every folder root's alone - the users' sandbox-ai, builds and app failed).
  umask 022
  data_dir="$prefix/lib/sandbox-ai"
  bin_dirs=("$prefix/bin")
else
  [[ -n "${HOME:-}" && -d "$HOME" && -w "$HOME" ]] || die "no writable HOME; use --system as root"
  data_dir="${XDG_DATA_HOME:-$HOME/.local/share}/sandbox-ai"
  bin_dirs=("$HOME/.local/bin")
  [[ -d "$HOME/.bin" ]] && bin_dirs+=("$HOME/.bin")
fi
[[ -n "$bin_override" ]] && bin_dirs=("$bin_override")

installed_version() {
  [[ -f "$data_dir/current/sandbox/agent_sandbox.py" ]] || return 0
  sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$data_dir/current/sandbox/agent_sandbox.py"
}

# The release keys of install.sh's own tree (a checkout, an unpacked bundle, the installed tree); none when
# the script comes through a pipe (curl ... | bash): then the embedded ones count.
script_signers=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  # Through the sandbox-ai-update link: the tree the link points into.
  script_signers="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/$SIGNERS"
fi
state_file="$data_dir/release-state.json"

# Network, JSON, checksums, signatures and safe unpacking: Python's standard library (and ssh-keygen) only.
# Bytecode for the installed version, once: the version folder is read-only to its users afterwards, so without it
# every sandbox-ai command compiled its large modules again (agent_sandbox.py and runtime.py: about 0.3 s each time;
# the app and the web interface run commands all the time). For python3 and the app's own Python when it has one.
precompile() {
  local tree=$1 python
  for python in python3 "$data_dir/app-env/bin/python3"; do
    command -v "$python" >/dev/null 2>&1 || [[ -x "$python" ]] || continue
    "$python" -I -m compileall -q -j 0 -x '/tests/' "$tree/sandbox" "$tree/projects" >/dev/null 2>&1 || true
  done
}

helper() {
  python3 -I - "$@" <<'PY'
import base64, hashlib, ipaddress, json, os, re, shutil, stat, subprocess, sys, tarfile, tempfile, time
import urllib.error, urllib.parse, urllib.request

ASSET = re.compile(r"sandbox-ai-bundle-([0-9]+\.[0-9]+\.[0-9]+(?:[.+-][0-9A-Za-z.+-]+)?)\.tar\.gz")
# The same rules as sandbox/release_keys.py (this script runs before any tree is installed).
NAMESPACE, PRINCIPAL = "sandbox-ai-release", "release@sandbox-ai"
KEY_LINE = re.compile(r'release@sandbox-ai\s+namespaces="sandbox-ai-release"\s+'
                      r'(ssh-ed25519|sk-ssh-ed25519@openssh\.com|ecdsa-sha2-nistp(?:256|384|521)|'
                      r'sk-ecdsa-sha2-nistp256@openssh\.com|ssh-rsa)\s+([A-Za-z0-9+/]+={0,2})(?:\s.*)?')


def fail(message):
    sys.exit(f"install.sh: {message}")


def note(message):
    print(f"install.sh: {message}", file=sys.stderr)


def warn(message):
    bar = "!" * 78
    text = f"{bar}\ninstall.sh: WARNING: {message}\n{bar}"
    if not os.environ.get("NO_COLOR") and (os.environ.get("FORCE_COLOR") or sys.stderr.isatty() and
                                           os.environ.get("TERM", "") != "dumb"):
        text = "\n".join(f"\033[33m{line}\033[0m" for line in text.split("\n"))  # (yellow, as install.sh's own)
    print(f"\n{text}\n", file=sys.stderr)


def read(path, mode="r"):
    with open(path, mode) as stream:
        return stream.read()


# --- release keys and signatures ------------------------------------------------------------------------


def parse_signers(text, origin):
    """[(type, base64 key)] of an allowed signers text. Anything but keys, comments and blank lines fails closed."""
    keys = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = KEY_LINE.fullmatch(line)
        try:
            blob = base64.b64decode(match[2], validate=True) if match else b""
        except ValueError:
            blob = b""
        if not match or blob[4:4 + int.from_bytes(blob[:4], "big")] != match[1].encode():
            fail(f'{origin}, line {number}: not a release key (release@sandbox-ai namespaces="{NAMESPACE}" TYPE KEY)')
        keys.append((match[1], match[2]))
    return keys


def fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(key)).digest()).decode().rstrip("=")


def signature_info(signature):
    """(namespace, key fingerprint) of an armored SSH signature, None if it is none."""
    match = re.search(rb"-----BEGIN SSH SIGNATURE-----(.*?)-----END SSH SIGNATURE-----", signature, re.S)
    try:
        blob = base64.b64decode(b"".join(match[1].split()), validate=True) if match else b""
    except ValueError:
        return None
    if blob[:6] != b"SSHSIG":
        return None
    fields, position = [], 10
    for _ in range(2):
        size = int.from_bytes(blob[position:position + 4], "big")
        position += 4
        if position + size > len(blob):
            return None
        fields.append(blob[position:position + size])
        position += size
    return fields[1].decode(errors="replace"), fingerprint(base64.b64encode(fields[0]))


class Trust:
    """The release keys this installation trusts: those of the installed tree; without any, the ones that came
    with install.sh (its own tree, or the lines make_dist.py embedded into the published install.sh)."""
    def __init__(self, installed, script, embedded, policy, allow_unsigned):
        self.policy, self.allow_unsigned = policy == "1", allow_unsigned == "1"
        self.keys, self.origin = [], "none"
        for path, origin in ((installed, "the installed tree"), (script, f"install.sh's tree ({script})")):
            if path and os.path.isfile(path):
                self.keys = parse_signers(read(path), path)
                if self.keys:
                    self.origin = origin
                    return
        self.keys = parse_signers(embedded, "install.sh's embedded release keys")
        if self.keys:
            self.origin = "the keys embedded in install.sh (trust on first use)"

    def fingerprints(self):
        return [fingerprint(key) for _, key in self.keys]

    def describe(self):
        return f"{', '.join(self.fingerprints()) or 'none'} ({self.origin})"


def verify(data, signature, trust):
    """('good', fingerprint) or ('bad', reason): checked by ssh-keygen -Y verify against the trusted keys."""
    info = signature_info(signature)
    if info is None:
        return "bad", "SHA256SUMS.sig is not an SSH signature"
    namespace, signer = info
    if namespace != NAMESPACE:
        return "bad", f"the signature is for {namespace!r}, not {NAMESPACE!r}"
    if signer not in trust.fingerprints():
        return "bad", f"signed by {signer}, which is not a trusted release key"
    keygen = shutil.which("ssh-keygen")
    if not keygen:
        fail("checking the release signature needs ssh-keygen (OpenSSH 8.1 or newer; Debian/Ubuntu: "
             "apt install openssh-client, Fedora: dnf install openssh)")
    with tempfile.TemporaryDirectory() as tmp:
        allowed, sig = os.path.join(tmp, "allowed_signers"), os.path.join(tmp, "SHA256SUMS.sig")
        with open(allowed, "w") as stream:
            stream.writelines(f'{PRINCIPAL} namespaces="{NAMESPACE}" {kind} {key}\n' for kind, key in trust.keys)
        with open(sig, "wb") as stream:
            stream.write(signature)
        result = subprocess.run([keygen, "-Y", "verify", "-f", allowed, "-I", PRINCIPAL, "-n", NAMESPACE, "-s", sig],
                                input=data, capture_output=True)
    if result.returncode == 0:
        return "good", signer
    detail = (result.stderr or result.stdout).decode(errors="replace").strip().splitlines()
    if any("unknown option" in line or "usage:" in line for line in detail):
        fail("this ssh-keygen cannot check signatures (ssh-keygen -Y verify): OpenSSH 8.1 or newer is needed")
    return "bad", f"the signature does not match SHA256SUMS ({detail[0] if detail else 'ssh-keygen failed'})"


def judge(sums, signature, trust, what):
    """The signature status of a release ('good FINGERPRINT', 'unchecked', 'unsigned'), or stop: a trusted key must
    have signed it, unless no key is trusted yet or --allow-unsigned says otherwise (never against the policy)."""
    if not trust.keys:
        if trust.policy:
            fail(f"the administrator's policy requires signed updates (require_signed_updates), but no release key "
                 f"is trusted; nothing installed")
        if signature is None:
            note(f"{what} is not signed and no release key is trusted yet: checked against SHA256SUMS only")
            return "unsigned"
        info = signature_info(signature)
        note(f"{what} is signed{' by ' + info[1] if info else ''}, but no release key is trusted yet: "
             "checked against SHA256SUMS only")
        return "unchecked"
    verdict = ("bad", f"{what} is not signed (the release has no SHA256SUMS.sig)") if signature is None \
        else verify(sums, signature, trust)
    if verdict[0] == "good":
        return f"good {verdict[1]}"
    if trust.policy or not trust.allow_unsigned:
        # (--verify asks for a good signature whatever --allow-unsigned says: naming that would send you in a circle)
        hint = "" if trust.policy else \
            ("; --verify installs only a bundle a trusted key signed (without --verify a local bundle is installed "
             "unchecked)") if getattr(trust, "verifying", False) else \
            "; --allow-unsigned installs it anyway (not recommended)"
        fail(f"{verdict[1]}; nothing installed. Trusted release keys: {trust.describe()}{hint}")
    warn(f"{verdict[1]}.\nInstalling it anyway because of --allow-unsigned: whoever controls the release, the "
         "account or the download could have changed it.")
    return "unsigned"


# --- versions -------------------------------------------------------------------------------------------


def version_key(version):
    match = re.match(r"([0-9]+)\.([0-9]+)\.([0-9]+)(.*)", version)
    return (int(match[1]), int(match[2]), int(match[3]), match[4] == "", match[4]) if match else (0, 0, 0, False, "")


def highest_seen(state):
    try:
        value = json.loads(read(state)).get("highest_seen", "")
        return value if isinstance(value, str) and ASSET.fullmatch(f"sandbox-ai-bundle-{value}.tar.gz") else ""
    except (OSError, ValueError, AttributeError):
        return ""


def cmd_record(state, version):
    """Remember the newest release this installation has installed or seen signed (rollback warnings)."""
    seen = highest_seen(state)
    if not os.path.isdir(os.path.dirname(state)) or (seen and version_key(seen) >= version_key(version)):
        return
    temporary = f"{state}.{os.getpid()}"
    try:
        with open(temporary, "w") as stream:
            json.dump({"highest_seen": version, "recorded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                      stream)
        os.replace(temporary, state)
    except OSError:
        # Not ours to write: a user's --check of a system installation (root's). The check itself stands; only root's
        # own check and update remember the newest release seen.
        try:
            os.unlink(temporary)
        except OSError:
            pass


def check_version(found, current, state, allow_downgrade, explicit):
    """No release older than the installed one (a source serving an old, vulnerable release), unless asked for."""
    if current and version_key(found) < version_key(current):
        if allow_downgrade != "1":
            why = "" if explicit else ", although the source lists it as the newest release (an old release served again?)"
            fail(f"sandbox-ai {found} is older than the installed {current}{why}; nothing installed. "
                 "--allow-downgrade installs it anyway")
        note(f"downgrading from {current} to {found} (--allow-downgrade)")
    seen = highest_seen(state)
    if not explicit and seen and version_key(found) < version_key(seen):
        note(f"the newest release of the source is {found}, but this installation has seen {seen} before: "
             "the source may be serving an old release")


# --- the releases API -----------------------------------------------------------------------------------


def api_base(source):
    url = urllib.parse.urlsplit(re.sub(r"\.git$", "", source.rstrip("/")))
    parts = [p for p in url.path.split("/") if p]
    if url.scheme not in ("http", "https") or len(parts) < 2 or not url.hostname:
        fail(f"--source must be http(s)://HOST/OWNER/REPO: {source!r}")
    owner, repo, root = parts[-2], parts[-1], "/".join(parts[:-2])
    if url.hostname in ("github.com", "www.github.com"):
        return "https://api.github.com/repos/{}/{}".format(owner, repo), url
    base = f"{url.scheme}://{url.netloc}" + (f"/{root}" if root else "")
    return f"{base}/api/v1/repos/{owner}/{repo}", url


def loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry the token to another host, nor over plain http but to this host's loopback."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        target, origin = urllib.parse.urlsplit(newurl), urllib.parse.urlsplit(req.full_url)
        if new is not None and (target.netloc != origin.netloc
                                or (target.scheme != "https" and not loopback(target.hostname or ""))):
            new.remove_header("Authorization")
        return new


def fetch(url, token_host, accept="application/json"):
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "sandbox-ai-installer"})
    parts = urllib.parse.urlsplit(url)
    token = os.environ.get("SANDBOX_AI_TOKEN")
    if token and parts.netloc == token_host:
        if parts.scheme != "https" and not loopback(parts.hostname or ""):
            fail(f"refusing to send SANDBOX_AI_TOKEN over plain http to {parts.hostname}")
        if not re.fullmatch(r"[A-Za-z0-9_.~+/=-]{8,512}", token):
            fail("SANDBOX_AI_TOKEN does not look like an access token")
        request.add_header("Authorization", f"token {token}")
    opener = urllib.request.build_opener(SameHostRedirect)
    try:
        with opener.open(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        limit, retry = exc.headers.get("X-RateLimit-Remaining"), exc.headers.get("Retry-After", "")
        if exc.code in (403, 429) and (limit == "0" or retry):
            reset = exc.headers.get("X-RateLimit-Reset", "")
            when = (f"after {time.strftime('%H:%M', time.localtime(int(reset)))}" if reset.isdigit()
                    else f"in {retry} seconds" if retry.isdigit() else "later")
            hint = "" if token else ("; GitHub allows 60 API requests an hour per address without a token: "
                                     "SANDBOX_AI_TOKEN with any token (no scopes needed for a public repository) "
                                     "raises that to 5000")
            fail(f"{parts.netloc} limits the requests to its API (HTTP {exc.code}); try again {when}{hint}")
        hint = " (private repository? set SANDBOX_AI_TOKEN)" if exc.code in (401, 403, 404) and not token else ""
        raise LookupError(f"{url}: HTTP {exc.code}{hint}") from None
    except (urllib.error.URLError, OSError) as exc:
        fail(f"cannot reach {parts.netloc}: {getattr(exc, 'reason', exc)}")


def release(source, version):
    """The release's version, bundle name and the (url, token host) of its bundle, SHA256SUMS and SHA256SUMS.sig."""
    api, url = api_base(source)
    github = api.startswith("https://api.github.com/")
    host = "api.github.com" if github else url.netloc
    try:
        if version:
            data = json.loads(fetch(f"{api}/releases/tags/v{version}", host))
        else:
            try:
                data = json.loads(fetch(f"{api}/releases/latest", host))
            except LookupError:
                # Older Gitea: newest published, non-pre-release from the list.
                listing = json.loads(fetch(f"{api}/releases?limit=20", host))
                data = next((r for r in listing if not r.get("draft") and not r.get("prerelease")), None)
                if data is None:
                    fail(f"{source} has no published release yet; to install or update from a checkout of it: "
                         "./install.sh --from DIR (DIR: the checkout, e.g. --from .)")
    except LookupError as exc:
        fail(str(exc))

    def link(asset):
        # A private GitHub repository serves files to a token only through the API (the download link on
        # github.com ignores it); public ones use the download link, which costs no API request.
        api_url = asset.get("url")
        if github and os.environ.get("SANDBOX_AI_TOKEN") and isinstance(api_url, str) \
                and api_url.startswith(f"{api}/releases/assets/"):
            return api_url, "api.github.com"
        return asset.get("browser_download_url"), url.netloc

    assets = {a.get("name"): link(a) for a in data.get("assets", []) if isinstance(a, dict)}
    bundles = [(m[1], name) for name in assets if isinstance(name, str) and (m := ASSET.fullmatch(name))]
    if not bundles or "SHA256SUMS" not in assets:
        fail(f"release {data.get('tag_name')!r} lacks sandbox-ai-bundle-*.tar.gz or SHA256SUMS")
    found, name = bundles[0]
    return found, name, assets[name], assets["SHA256SUMS"], assets.get("SHA256SUMS.sig")


def download(asset):
    try:
        return fetch(*asset, accept="application/octet-stream")
    except LookupError as exc:
        fail(str(exc))


def checksums(text):
    return {parts[1].lstrip("*"): parts[0] for parts in (line.split() for line in text.splitlines()) if len(parts) == 2}


def cmd_check(source, version, current, state, *trust):
    trust = Trust(*trust)
    found, name, _, sums_url, sig_url = release(source, version)
    print(f"available: {found} ({source})")
    if current and version_key(found) < version_key(current):
        print(f"           older than the installed {current}: an update refuses it without --allow-downgrade")
    sums, signature = download(sums_url), download(sig_url) if sig_url else None
    if signature is None:
        status = "none (the release has no SHA256SUMS.sig)"
    elif not trust.keys:
        info = signature_info(signature)
        status = f"by {info[1] if info else 'an unreadable key'}, not checked: no release key is trusted yet"
    else:
        verdict, detail = verify(sums, signature, trust)
        status = f"good, by the trusted key {detail}" if verdict == "good" else f"BAD: {detail}"
        if verdict == "good":
            cmd_record(state, found)
    print(f"signature: {status}")
    print(f"trusted keys: {trust.describe()}")
    if trust.policy:
        print("policy: signed updates required (require_signed_updates)")
    if trust.keys and not status.startswith("good"):
        print("           an update refuses this release" + ("" if trust.policy else " without --allow-unsigned"))


def cmd_download(source, version, target, current, state, allow_downgrade, *trust):
    trust = Trust(*trust)
    found, name, bundle_url, sums_url, sig_url = release(source, version)
    check_version(found, current, state, allow_downgrade, bool(version))
    sums = download(sums_url)
    status = judge(sums, download(sig_url) if sig_url else None, trust, f"the release {found}")
    data = download(bundle_url)
    if checksums(sums.decode(errors="replace")).get(name) != hashlib.sha256(data).hexdigest():
        fail(f"checksum mismatch for {name}; nothing installed")
    with open(target, "wb") as stream:
        stream.write(data)
    print(found)
    print(status)


def cmd_fetch(source, version, directory, current, state, *trust):
    """--fetch DIR: the release downloaded and checked as an update checks it - and more strictly: a trusted key must
    have signed it (never unsigned, whatever --allow-unsigned says), never older than the installed one - then kept
    in DIR as it came (the bundle, SHA256SUMS, SHA256SUMS.sig), for --from DIR/BUNDLE --verify to check again and
    install. Prints the version, the signature status and the bundle's name."""
    trust = Trust(*trust)
    if not trust.keys:
        fail("--fetch needs a trusted release key (the installed tree lists none): nothing downloaded")
    found, name, bundle_url, sums_url, sig_url = release(source, version)
    check_version(found, current, state, "0", bool(version))
    sums = download(sums_url)
    if sig_url is None:
        fail(f"the release {found} is not signed (no SHA256SUMS.sig); nothing downloaded")
    signature = download(sig_url)
    verdict, detail = verify(sums, signature, trust)
    if verdict != "good":
        fail(f"{detail}; nothing downloaded. Trusted release keys: {trust.describe()}")
    data = download(bundle_url)
    if checksums(sums.decode(errors="replace")).get(name) != hashlib.sha256(data).hexdigest():
        fail(f"checksum mismatch for {name}; nothing downloaded")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    for file, content in ((name, data), ("SHA256SUMS", sums), ("SHA256SUMS.sig", signature)):
        temporary = os.path.join(directory, f".{file}.{os.getpid()}")
        with open(temporary, "wb") as stream:
            stream.write(content)
        os.replace(temporary, os.path.join(directory, file))
    cmd_record(state, found)
    print(found)
    print(f"good {detail}")
    print(name)


def cmd_verify(bundle, given, *trust):
    """--from BUNDLE --verify: the SHA256SUMS and SHA256SUMS.sig next to it, like a download."""
    trust, directory, name = Trust(*trust), os.path.dirname(os.path.abspath(given)), os.path.basename(given)
    sums_path = os.path.join(directory, "SHA256SUMS")
    if not os.path.isfile(sums_path):
        fail(f"--verify: no SHA256SUMS next to {given}")
    if not trust.keys:
        fail("--verify: no release key is trusted (neither the installed tree nor install.sh lists one)")
    sums = read(sums_path, "rb")
    signature = read(sums_path + ".sig", "rb") if os.path.isfile(sums_path + ".sig") else None
    trust.allow_unsigned, trust.verifying = False, True
    status = judge(sums, signature, trust, name)
    if checksums(sums.decode(errors="replace")).get(name) != hashlib.sha256(read(bundle, "rb")).hexdigest():
        fail(f"checksum mismatch for {name} (or SHA256SUMS does not list it); nothing installed")
    print(status)


def cmd_accept(tree, status, *trust):
    """The new tree's release keys take over from the next update on: after a good signature by a trusted key
    (that is how keys are rotated), never through a release no trusted key signed."""
    trust = Trust(*trust)
    path = os.path.join(tree, "sandbox", "release-signers")
    new = parse_signers(read(path), "the new release's sandbox/release-signers") if os.path.isfile(path) else []
    old_prints, new_prints = trust.fingerprints(), [fingerprint(key) for _, key in new]
    if set(old_prints) == set(new_prints):
        return
    if not status.startswith("good") and old_prints:
        fail("this release is not signed by a trusted key and would change the trusted release keys "
             f"({', '.join(old_prints)} -> {', '.join(new_prints) or 'none'}); nothing installed")
    if not old_prints:
        note(f"from now on, updates must be signed by: {', '.join(new_prints)} (the keys of this release: trust on "
             "first use)")
        return
    added, removed = sorted(set(new_prints) - set(old_prints)), sorted(set(old_prints) - set(new_prints))
    note("the release keys change with this version (signed by a trusted key): "
         + "; ".join(filter(None, [added and "added " + ", ".join(added), removed and "removed " + ", ".join(removed)])))
    if not new_prints:
        warn("this release lists no release key: later updates are no longer checked for a signature")


def cmd_policy(path, owner):
    """1 when the administrator's policy demands signed updates. Like the launcher's policy: only a file root owns
    and only root can change counts, and a broken or unsafe one stops (fail closed)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        print(0)
        return
    except OSError as exc:
        fail(f"policy {path}: {exc.strerror}")
    for item, label in ((info, path), (os.stat(os.path.dirname(path)), os.path.dirname(path))):
        if item.st_uid != int(owner) or item.st_mode & 0o022:
            fail(f"policy {label} must be owned by root and writable by root only")
    if not stat.S_ISREG(info.st_mode):
        fail(f"policy {path} must be a regular file")
    try:
        policy = json.loads(read(path))
    except (OSError, ValueError, RecursionError) as exc:
        fail(f"policy {path} is not valid JSON: {exc}")
    value = policy.get("require_signed_updates", False) if isinstance(policy, dict) else None
    if not isinstance(value, bool):
        fail(f"policy {path}: require_signed_updates is true or false")
    print(int(value))


def cmd_unpack(archive, target):
    """Regular files and directories below one top directory only: no links, devices or escapes."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        tops = {m.name.split("/", 1)[0] for m in members}
        if len(tops) != 1:
            fail("bundle must contain exactly one top directory")
        top = tops.pop()
        for m in members:
            parts = m.name.split("/")
            if m.name.startswith("/") or ".." in parts or not (m.isfile() or m.isdir()):
                fail(f"unsafe entry in bundle: {m.name!r}")
        os.makedirs(target)
        for m in members:
            relative = m.name[len(top):].lstrip("/")
            path = os.path.join(target, relative)
            if m.isdir():
                os.makedirs(path, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with tar.extractfile(m) as src, open(path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.chmod(path, 0o755 if m.mode & 0o111 else 0o644)


def cmd_copy(checkout, target):
    """A checkout: tracked and new, not ignored files; without git everything except caches and VCS data."""
    import subprocess
    try:
        # Only a checkout's own repository: git would otherwise look in the folders above (an upload folder in
        # /tmp under a planted /tmp/.git, whose core.fsmonitor runs a command), and never past it.
        if not os.path.exists(os.path.join(checkout, ".git")):
            raise OSError("not a git checkout")
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": os.path.dirname(os.path.abspath(checkout))}
        # safe.directory: root installing from a user's checkout (./install.sh --from . with sudo) - git refuses a
        # repository of another owner ("dubious ownership") and the copy fell back to every file, sockets too.
        safe = ["-c", "safe.directory=" + os.path.abspath(checkout)]
        names = subprocess.run(["git", *safe, "-C", checkout, "ls-files", "-z", "--cached", "--others",
                                "--exclude-standard"],
                               capture_output=True, check=True, text=True, env=env).stdout.split("\0")
        names = sorted({n for n in names if n and not n.startswith((".claude/", ".git/"))
                        and os.path.isfile(os.path.join(checkout, n))})
    except (OSError, subprocess.CalledProcessError):
        names = []
    if not names:
        skip = {".git", "__pycache__", ".pytest_cache", "dist", "build", ".claude", ".tt"}
        for root, dirs, files in os.walk(checkout):
            dirs[:] = [d for d in dirs if d not in skip and not d.endswith(".egg-info")]
            # Regular files only: a socket or FIFO (a test's leftover) cannot be copied and is no part of it.
            names += [os.path.relpath(os.path.join(root, f), checkout) for f in files
                      if os.path.isfile(os.path.join(root, f))]
    app = os.path.join("projects", "sandbox-ai-app")
    if os.path.isfile(os.path.join(checkout, app, "sandbox_ai_app", "__main__.py")):
        # Its own repository inside the checkout (git lists it as one directory): its tracked and new files.
        try:
            listed = subprocess.run(["git", "-c", "safe.directory=" + os.path.abspath(os.path.join(checkout, app)),
                                     "-C", os.path.join(checkout, app), "ls-files", "-z", "--cached",
                                     "--others", "--exclude-standard"],
                                    capture_output=True, check=True, text=True).stdout.split("\0")
        except (OSError, subprocess.CalledProcessError):
            listed = []
            for root, dirs, files in os.walk(os.path.join(checkout, app)):
                dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".pytest_cache", "dist", "build"}
                           and not d.endswith(".egg-info")]
                listed += [os.path.relpath(os.path.join(root, f), os.path.join(checkout, app)) for f in files
                           if os.path.isfile(os.path.join(root, f))]
        names = sorted(set(names) | {os.path.join(app, n) for n in listed if n and not n.startswith(".git/")
                                     and os.path.isfile(os.path.join(checkout, app, n))})
    # The MCP catalog of sandbox-ai-mcp (its own repository too): sandbox-ai mcp reads it before the web.
    # With its signature and rootfs.json (and that one's): without them a signed catalog reads as unsigned.
    catalog = os.path.join("projects", "sandbox-ai-mcp", "catalog.json")
    if os.path.isfile(os.path.join(checkout, catalog)) and not os.path.islink(os.path.join(checkout, catalog)):
        extra = [catalog, catalog + ".sig", os.path.join("projects", "sandbox-ai-mcp", "rootfs.json"),
                 os.path.join("projects", "sandbox-ai-mcp", "rootfs.json.sig")]
        names = sorted(set(names) | {n for n in extra if os.path.isfile(os.path.join(checkout, n))
                                     and not os.path.islink(os.path.join(checkout, n))})
    for name in names:
        source, path = os.path.join(checkout, name), os.path.join(target, name)
        if os.path.islink(source):
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(source, path)
        os.chmod(path, 0o755 if os.stat(source).st_mode & 0o111 else 0o644)


def invoking_user(source, environ=os.environ, getpwnam=None, owner_of=None):
    """(name, home, uid) of the user a system installation runs for: sudo's SUDO_USER, else - a root login in a
    user's folder - the owner of the --from tree SOURCE; None when it is root's own."""
    import pwd
    getpwnam = getpwnam or pwd.getpwnam
    name = environ.get("SUDO_USER") or ""
    if not name or name == "root":
        try:
            uid = (owner_of or (lambda path: os.stat(path).st_uid))(source) if source else 0
            name = pwd.getpwuid(uid).pw_name if uid else ""
        except (OSError, KeyError):
            name = ""
    if not name or name == "root":
        return None
    try:
        entry = getpwnam(name)
    except KeyError:
        return None
    return entry.pw_name, entry.pw_dir, entry.pw_uid


def cmd_invoking_user(source=""):
    found = invoking_user(source)
    if found:
        print("\t".join(map(str, found)))


def user_note(source, version, environ=os.environ, getpwnam=None, owner_of=None):
    """As root (a system installation): the line about the invoking user's own installation in
    ~/.local/share/sandbox-ai, which stays at its version (and may come first in their PATH), or None."""
    found = invoking_user(source, environ, getpwnam, owner_of)
    if not found:
        return None
    name, home, _ = found
    data = os.path.join(home, ".local", "share", "sandbox-ai")
    own = None
    try:
        with open(os.path.join(data, "current", "sandbox", "agent_sandbox.py"), encoding="utf-8",
                  errors="replace") as stream:
            for line in stream:  # (some 600 lines down)
                own = re.match(r'__version__ = "([^"\n]{1,40})"$', line.rstrip("\n"))
                if own:
                    break
    except OSError:
        return None
    own = own.group(1) if own else "?"
    if own == version:
        return None
    update = f"./install.sh --from {source}" if source else os.path.join(data, "current", "install.sh")
    return (f"warning: {name} has an installation of their own, sandbox-ai {own} in {data}: it stays at {own}, and "
            f"their PATH may find it first (~/.local/bin); update it as {name}: {update} (or remove it as {name}: "
            f"{os.path.join(data, 'current', 'install.sh')} --uninstall)")


def cmd_user_note(source, version):
    line = user_note(source, version)
    if line:
        print(line)


{"check": cmd_check, "fetch": cmd_fetch, "download": cmd_download, "verify": cmd_verify, "accept": cmd_accept, "record": cmd_record,
 "policy": cmd_policy, "unpack": cmd_unpack, "copy": cmd_copy, "invoking_user": cmd_invoking_user,
 "user_note": cmd_user_note}[sys.argv[1]](*sys.argv[2:])
PY
}

skipped_links=()  # (said again at the end: end_notes)
link_commands() {
  local dir cmd target link
  for dir in "${bin_dirs[@]}"; do
    mkdir -p "$dir"
    for cmd in "${OLD_COMMANDS[@]}"; do
      link="$dir/$cmd"
      if [[ -L "$link" && $(readlink "$link") == "$data_dir"/* ]]; then
        rm -f "$link"
        note "removed $link: use sandbox-ai instead (see sandbox-ai --help)"
      fi
    done
    for cmd in "${COMMANDS[@]}" sandbox-ai-update $(app_linked && echo "$APP_COMMAND"); do
      link="$dir/$cmd"
      if [[ $cmd == sandbox-ai-update ]]; then target="$data_dir/current/install.sh"; else target="$data_dir/current/$cmd"; fi
      if [[ -e "$link" || -L "$link" ]] && ! { [[ -L "$link" ]] && [[ $(readlink "$link") == "$data_dir"/* ]]; }; then
        if [[ $force == 1 ]]; then
          rm -f "$link"
        else
          note "skipping $link: exists and does not belong to sandbox-ai (use --force to replace)"
          skipped_links+=("$link")
          continue
        fi
      fi
      ln -sfn "$target" "$link"
    done
  done
}

app_linked() {
  [[ $app == 1 && -f "$data_dir/current/$APP_COMMAND" \
     && -f "$data_dir/current/projects/sandbox-ai-app/sandbox_ai_app/__main__.py" ]]
}

# PySide6 for the app: in its own environment below the installation, never in the system's Python.
app_dependencies() {
  local tree=$1 env="$data_dir/app-env" want=() python=python3 modules="PySide6.QtWidgets"
  local app_dir="$tree/projects/sandbox-ai-app"
  [[ $app == 1 && -f "$app_dir/sandbox_ai_app/__main__.py" ]] || return 0
  if [[ $app_deps == no ]]; then app_hint; app_xterm "$app_dir"; return 0; fi
  [[ $app_terminal == 1 ]] && modules="$modules, PySide6.QtWebEngineWidgets"
  # python3 itself is enough when it has a working PySide6 (with QtWebEngine for terminal tabs).
  if [[ $app_deps == auto && ! -x "$env/bin/python3" ]] \
     && python3 -I -c "import sys, $modules; sys.exit(PySide6.__version__ == '6.12.0' and sys.version_info < (3, 12))" 2>/dev/null; then
    app_xterm "$app_dir"
    return 0
  fi
  want=("${APP_PACKAGES[@]}")
  [[ $app_terminal == 1 ]] && want+=("${APP_TERMINAL_PACKAGES[@]}")
  if [[ ! -x "$env/bin/python3" ]] && ! python3 -I -m venv "$env" >/dev/null 2>&1; then
    rm -rf -- "$env"
    note "cannot create the app's environment (python3 -m venv; Debian/Ubuntu: apt install python3-venv)"
    app_hint
    return 0
  fi
  if [[ $app_terminal == 1 ]]; then
    note "installing PySide6 with QtWebEngine for sandbox-ai-app into $env (from PyPI, about 300 MB)"
  else
    note "installing PySide6 for sandbox-ai-app into $env (from PyPI, about 100 MB)"
  fi
  if ! "$env/bin/python3" -I -m pip install --disable-pip-version-check -q --upgrade "${want[@]}"; then
    note "pip could not install PySide6; sandbox-ai is installed, the app needs: sandbox-ai-update --force --app-deps"
    return 0
  fi
  python="$env/bin/python3"
  "$python" -I -c "import PySide6.QtWidgets" 2>/dev/null \
    || note "PySide6 is installed but Qt does not load: install the OpenGL/EGL, xkbcommon and fontconfig libraries (Debian/Ubuntu: libegl1 libgl1 libxkbcommon0 libfontconfig1 libgssapi-krb5-2)"
  if [[ $app_terminal == 1 ]] && ! "$python" -I -c "import PySide6.QtWebEngineWidgets" 2>/dev/null; then
    note "QtWebEngine does not load (Debian/Ubuntu also need: libnss3 libxcomposite1 libxdamage1 libxrandr2 libxtst6 libasound2); terminal tabs fall back to terminal windows"
  fi
  app_xterm "$app_dir"
}

# xterm.js for the terminal tabs, from registry.npmjs.org, checked against the app's vendor/xterm.json.
app_xterm() {
  local app_dir=$1
  [[ $app_terminal == 1 ]] || return 0
  [[ -f "$app_dir/sandbox_ai_app/web/vendor/xterm.js" && -f "$app_dir/sandbox_ai_app/web/vendor/manifest.json" ]] && return 0
  note "fetching xterm.js for the app's terminal tabs (registry.npmjs.org, checked)"
  python3 -I "$app_dir/scripts/vendor_xterm.py" \
    || note "xterm.js could not be downloaded (see above); the app opens commands in terminal windows until it is (retry: sandbox-ai-update --force, or the button on the app's Terminals page)"
}

app_hint() {
  python3 -I -c "import PySide6.QtWidgets" 2>/dev/null || [[ -x "$data_dir/app-env/bin/python3" ]] \
    || note "sandbox-ai-app needs PySide6: sandbox-ai-update --force --app-deps (or pip install --user PySide6-Essentials)"
}

# The end of an installation: what may still keep the human on another version (2026-11-18: "I ran install.sh --from .
# several times, but sandbox-ai-app says 0.1.97"): a command link left out for a foreign file (said again here, where
# it is not missed), another installation that PATH finds first (a user installation: the PATH a new shell has),
# an app that runs an older version, and - as root - the invoking user's own installation, which stays as it is.
end_notes() {
  local line tree="$data_dir/current" path=$PATH commands=() sockets=() who="" home="" uid=""
  for line in ${skipped_links[@]+"${skipped_links[@]}"}; do
    note "warning: not linked: $line exists and does not belong to sandbox-ai (install.sh --force replaces it)"
  done
  commands=("${COMMANDS[@]}" sandbox-ai-update)
  if app_linked; then commands+=("$APP_COMMAND"); fi
  if [[ $mode == user && -f "$tree/sandbox/path_order.py" ]]; then
    if [[ $modify_rc == 1 ]]; then
      case ":$PATH:" in *":${bin_dirs[0]}:"*) ;; *) path="${bin_dirs[0]}:$PATH" ;; esac
    fi
    while IFS= read -r line; do
      [[ -n "$line" ]] && note "$line"
    done < <(python3 -I "$tree/sandbox/path_order.py" --mine "$tree" --folder "${bin_dirs[0]}" \
               ${from:+--from "$from"} --path "$path" "${commands[@]}" 2>/dev/null || true)
  fi
  if [[ $mode == system ]]; then
    IFS=$'\t' read -r who home uid < <(helper invoking_user "$from" 2>/dev/null || true) || true
    line=$(helper user_note "$from" "$version" 2>/dev/null || true)
    [[ -z "$line" ]] || note "$line"
    if [[ -n "$who" && -n "$home" ]]; then
      sockets=("/run/user/$uid/sandbox-ai-app.sock" "$home/.local/state/sandbox-ai/sandbox-ai-app.sock")
    fi
  fi
  if app_linked && [[ $mode == user || ${#sockets[@]} -gt 0 ]]; then
    # The installed copy's instance_probe.py asks the app on the single-instance socket what it is.
    line=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from sandbox_ai_app import instance_probe
sys.exit(instance_probe.main(sys.argv[2:]))' "$tree/projects/sandbox-ai-app" ${sockets[@]+"${sockets[@]}"} \
             2>/dev/null || true)
    [[ -z "$line" ]] || note "warning: $line"
  fi
  return 0
}

# A menu entry for user installations (removed by --uninstall).
app_desktop_entry() {
  local file="${XDG_DATA_HOME:-$HOME/.local/share}/applications/sandbox-ai-app.desktop"
  [[ $mode == user ]] || return 0
  if ! app_linked; then rm -f -- "$file"; return 0; fi
  mkdir -p "$(dirname -- "$file")"
  printf '%s\n' "[Desktop Entry]" "Type=Application" "Name=sandbox-ai" "GenericName=AI agent sandboxes" \
    "Comment=Start, watch and answer AI agents in sandboxes" "Exec=${bin_dirs[0]}/$APP_COMMAND" \
    "Icon=utilities-terminal" "Terminal=false" "Categories=Development;Security;" > "$file"
}

path_hint() {
  local dir
  for dir in "${bin_dirs[@]}"; do
    case ":$PATH:" in *":$dir:"*) return 0 ;; esac
  done
  note "add ${bin_dirs[0]} to your PATH, e.g.: echo 'export PATH=\"${bin_dirs[0]}:\$PATH\"' >> ~/.bashrc"
}

# The shell start files of a user install: ~/.bashrc always, ~/.zshrc when there is one. Each gets one block
# between markers that puts the command directory on PATH unless it is there already; a new install replaces
# the block (another --bin-dir), --uninstall removes it, --no-modify-rc leaves the files alone. Written in place
# (a ~/.bashrc that is a link into a dotfiles repository stays one).
RC_BEGIN="# >>> sandbox-ai >>>"
RC_END="# <<< sandbox-ai <<<"
shell_rc_files() {
  if [[ $(uname -s) == Darwin ]]; then
    # macOS: zsh is the default shell (a new Mac has no ~/.zshrc yet); bash reads ~/.bash_profile at login.
    printf '%s\n' "$HOME/.zshrc"
    [[ -f "$HOME/.bash_profile" ]] && printf '%s\n' "$HOME/.bash_profile"
    [[ -f "$HOME/.bashrc" ]] && printf '%s\n' "$HOME/.bashrc"
    return 0
  fi
  printf '%s\n' "$HOME/.bashrc"
  [[ -f "$HOME/.zshrc" ]] && printf '%s\n' "$HOME/.zshrc"
  return 0
}

# Tab completion (sandbox-ai completion): where bash (with bash-completion), zsh (on its fpath) and fish (always)
# find the scripts - a user's under XDG_DATA_HOME and XDG_CONFIG_HOME, a system installation's under PREFIX/share
# (bash-completion, zsh's site-functions and fish's vendor completions look there); a user's rc block sources them
# for a bash without bash-completion and a zsh whose compinit ran.
completion_files() {
  if [[ $mode == system ]]; then
    printf '%s\n' "$prefix/share/bash-completion/completions/sandbox-ai" "$prefix/share/zsh/site-functions/_sandbox-ai" \
      "$prefix/share/fish/vendor_completions.d/sandbox-ai.fish"
    return 0
  fi
  local data="${XDG_DATA_HOME:-$HOME/.local/share}" config="${XDG_CONFIG_HOME:-$HOME/.config}"
  printf '%s\n' "$data/bash-completion/completions/sandbox-ai" "$data/zsh/site-functions/_sandbox-ai" \
    "$config/fish/completions/sandbox-ai.fish"
}

completion_state() {  # what the completion files hold now (to say only what changed)
  local file
  while IFS= read -r file; do
    if [[ -f $file ]]; then cksum < "$file"; else echo none; fi
  done < <(completion_files)
}

completion_changed=0
completion_setup() {
  local before shell file
  before=$(completion_state)
  if [[ $mode == user ]]; then
    # (install.sh keeps the rc files itself: its block, or none with --no-modify-rc)
    "${bin_dirs[0]}/sandbox-ai" completion install --no-rc >/dev/null 2>&1 || {
      note "Tab completion could not be set up (run: sandbox-ai completion install)"; return 0; }
  else
    for shell in bash zsh fish; do
      case $shell in
        bash) file="$prefix/share/bash-completion/completions/sandbox-ai" ;;
        zsh) file="$prefix/share/zsh/site-functions/_sandbox-ai" ;;
        fish) file="$prefix/share/fish/vendor_completions.d/sandbox-ai.fish" ;;
      esac
      mkdir -p -- "$(dirname -- "$file")"
      if [[ -L $file ]] || ! "${bin_dirs[0]}/sandbox-ai" completion "$shell" > "$file.new" 2>/dev/null; then
        rm -f -- "$file.new"
        note "Tab completion for $shell could not be written to $file (run: sandbox-ai completion $shell > $file)"
        continue
      fi
      chmod 644 "$file.new" && mv -f -- "$file.new" "$file"
    done
  fi
  [[ "$(completion_state)" != "$before" ]] || return 0
  completion_changed=1
  note "Tab completion for bash, zsh and fish: $(completion_files | tr '\n' ' ')"
}

shell_rc_block() {  # FILE DIR (no DIR: remove the block)
  local data="${XDG_DATA_HOME:-$HOME/.local/share}"
  python3 -I - "$1" "$RC_BEGIN" "$RC_END" "${2:-}" "$data/bash-completion/completions/sandbox-ai" \
    "$data/zsh/site-functions" <<'PY'
import os, shlex, sys
path, begin, end, directory, bash_completion, zsh_functions = sys.argv[1:7]
try:
    with open(path, encoding="utf-8", errors="surrogateescape") as stream:
        text = stream.read()
except FileNotFoundError:
    if not directory:
        sys.exit(0)
    text = ""
lines, kept, inside = text.splitlines(keepends=True), [], False
for line in lines:
    if line.rstrip("\n") == begin:
        inside = True
    elif inside and line.rstrip("\n") == end:
        inside = False
    elif not inside:
        kept.append(line)
if inside:  # a begin without its end: change nothing, say so
    sys.exit(f"{path}: a {begin!r} line without {end!r}; fix it by hand")
new = "".join(kept)
if directory:
    quoted = shlex.quote(directory)
    if new and not new.endswith("\n"):
        new += "\n"
    new += (f"{begin}\n"
            "# added by sandbox-ai's install.sh (removed by install.sh --uninstall)\n"
            f"sandbox_ai_bin={quoted}\n"
            'case ":$PATH:" in *":$sandbox_ai_bin:"*) ;; *) export PATH="$sandbox_ai_bin:$PATH" ;; esac\n'
            "unset sandbox_ai_bin\n"
            "# Tab completion (sandbox-ai completion)\n"
            f'if [ -n "${{BASH_VERSION:-}}" ] && [ -r {shlex.quote(bash_completion)} ]; then\n'
            f"  complete -p sandbox-ai >/dev/null 2>&1 || . {shlex.quote(bash_completion)}\n"
            f'elif [ -n "${{ZSH_VERSION:-}}" ] && [ -d {shlex.quote(zsh_functions)} ]; then\n'
            f"  fpath=({shlex.quote(zsh_functions)} $fpath)\n"
            f"  if typeset -f compdef >/dev/null 2>&1; then . {shlex.quote(zsh_functions + '/_sandbox-ai')}; fi\n"
            "fi\n"
            f"{end}\n")
if new != text:
    with open(path, "w", encoding="utf-8", errors="surrogateescape") as stream:
        stream.write(new)
    print("changed")
PY
}

rc_changed=()
shell_rc_setup() {
  [[ $mode == user && $modify_rc == 1 ]] || { path_hint; return 0; }
  local rc
  while IFS= read -r rc; do
    if [[ $(shell_rc_block "$rc" "${bin_dirs[0]}") == changed ]]; then rc_changed+=("${rc/#$HOME/\~}"); fi
  done < <(shell_rc_files)
  [[ ${#rc_changed[@]} -gt 0 ]] || return 0
  note "PATH: ${bin_dirs[0]} in ${rc_changed[*]} (a marked block; --no-modify-rc leaves the files alone)"
}

# What a terminal open now does to get it: a program cannot change a shell that already runs (its PATH, its
# completions); new terminals read the rc file (and the completion) themselves.
running_shell_hint() {
  local rc="" file shell_name=${SHELL##*/} on_path=1 completion=""
  case ":$PATH:" in *":${bin_dirs[0]}:"*) ;; *) on_path=0 ;; esac
  if [[ ${#rc_changed[@]} -eq 0 && $completion_changed == 0 && ( $on_path == 1 || $mode == system ) ]]; then return 0; fi
  if [[ $mode == user && $modify_rc == 1 ]]; then
    while IFS= read -r file; do
      file=${file/#$HOME/\~}
      [[ -z "$rc" ]] && rc=$file
      if [[ $shell_name == zsh && $file == *zshrc ]] || [[ $shell_name != zsh && $file == *bash* ]]; then rc=$file; break; fi
    done < <(shell_rc_files)
    if [[ ${#rc_changed[@]} -gt 0 || $completion_changed == 1 || $on_path == 0 ]]; then
      say hint "in a terminal open now run: source $rc (new terminals have it; a program cannot change a shell that already runs)"
    fi
    return 0
  fi
  [[ $completion_changed == 1 ]] || return 0
  case $shell_name in
    zsh) completion=$(completion_files | sed -n 2p) ;;
    fish) completion=$(completion_files | sed -n 3p) ;;
    *) completion=$(completion_files | sed -n 1p) ;;
  esac
  say hint "Tab completion in a terminal open now: source ${completion/#$HOME/\~} (a program cannot change a shell that already runs)"
}

# What --uninstall removes, one "KIND<TAB>PATH" line each (--uninstall --dry-run prints them; sandbox-ai uninstall
# shows them with their sizes before it asks, then runs --uninstall): the command links into this installation, its
# own files in DATA (never the data sandbox-ai keeps beside them in a user install), the menu entry, the app's login
# entry when it starts this installation, the PATH blocks and the Tab completion files.
UNINSTALL_FILES=(versions current app-env release-state.json update-pending)
uninstall_targets() {
  local dir cmd name file rc
  for dir in "${bin_dirs[@]}"; do
    for cmd in "${COMMANDS[@]}" "${OLD_COMMANDS[@]}" sandbox-ai-update "$APP_COMMAND"; do
      if [[ -L "$dir/$cmd" && $(readlink "$dir/$cmd") == "$data_dir"/* ]]; then printf 'link\t%s\n' "$dir/$cmd"; fi
    done
  done
  for name in "${UNINSTALL_FILES[@]}"; do
    if [[ -e "$data_dir/$name" || -L "$data_dir/$name" ]]; then printf 'installation\t%s\n' "$data_dir/$name"; fi
  done
  for file in "$data_dir"/.install-*; do
    if [[ -e "$file" || -L "$file" ]]; then printf 'installation\t%s\n' "$file"; fi
  done
  if [[ $mode == user ]]; then
    file="${XDG_DATA_HOME:-$HOME/.local/share}/applications/sandbox-ai-app.desktop"
    if [[ -e "$file" || -L "$file" ]]; then printf 'menu\t%s\n' "$file"; fi
    # The app's login entry (its settings: start at login), when it starts this installation's app: gone with it.
    for file in "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/sandbox-ai-app.desktop" \
                "$HOME/Library/LaunchAgents/org.sandbox-ai.app.plist"; do
      if [[ -f "$file" && ! -L "$file" ]] && grep -qF -- "$data_dir/" "$file"; then printf 'login\t%s\n' "$file"; fi
    done
  fi
  if [[ $mode == user && $modify_rc == 1 ]]; then
    while IFS= read -r rc; do
      if [[ -f "$rc" ]] && grep -qxF -- "$RC_BEGIN" "$rc"; then printf 'rc\t%s\n' "$rc"; fi
    done < <(shell_rc_files)
  fi
  while IFS= read -r file; do
    if [[ -f $file && ! -L $file ]] && grep -q "sandbox-ai __complete" "$file"; then printf 'completion\t%s\n' "$file"; fi
  done < <(completion_files)
  return 0
}

if [[ $action != uninstall ]]; then
  policy_signed=$(helper policy "$POLICY_FILE" "$POLICY_OWNER")
  if [[ $policy_signed == 1 && $allow_unsigned == 1 ]]; then
    die "--allow-unsigned is not allowed by the administrator's policy ($POLICY_FILE: require_signed_updates)"
  fi
  # Who may sign: the installed tree's keys, else install.sh's own (Trust in the helper).
  trust=("$data_dir/current/$SIGNERS" "$script_signers" "$EMBEDDED_SIGNERS" "$policy_signed" "$allow_unsigned")
fi

case "$action" in
  fetch)
    [[ -z "$from" ]] || die "--fetch downloads a release; --from installs one"
    helper fetch "$source_url" "$want_version" "$fetch_dir" "$(installed_version)" "$state_file" "${trust[@]}"
    exit 0 ;;
  check)
    current=$(installed_version)
    echo "installed: ${current:-none} ($data_dir)"
    if [[ -n "$from" ]]; then
      echo "available: (from $from)"
    else
      helper check "$source_url" "$want_version" "$current" "$state_file" "${trust[@]}"
    fi
    exit 0 ;;
  uninstall)
    if [[ ! -d "$data_dir/versions" && ! -e "$data_dir/current" ]]; then
      note "no installation in $data_dir: nothing removed (a user's own installation: --user; a system one: as root)"
      exit 0
    fi
    if [[ $dry_run == 1 ]]; then uninstall_targets; exit 0; fi
    # Only what this script made (uninstall_targets). In a user install $data_dir is also where sandbox-ai keeps your
    # secrets (file store), saved logins, kits, plugins, VPN keys and MCP rootfs trees: those stay (sandbox-ai
    # uninstall --purge removes them).
    targets=()
    while IFS= read -r line; do targets+=("$line"); done < <(uninstall_targets)
    for line in ${targets[@]+"${targets[@]}"}; do
      kind=${line%%$'\t'*} path=${line#*$'\t'}
      case $kind in
        installation)
          # installed versions are read-only (go-w, and app files)
          [[ -L "$path" ]] || chmod -R u+w -- "$path" 2>/dev/null || true
          rm -rf -- "$path" ;;
        rc) shell_rc_block "$path" >/dev/null || true ;;
        *) rm -f -- "$path" ;;
      esac
    done
    rmdir -- "$data_dir" 2>/dev/null || true  # gone if nothing else was in it (a system install's lib dir)
    if [[ -d "$data_dir" ]]; then
      note "removed the installation in $data_dir, its command links, the PATH block and the Tab completion; your data there (secrets, logins, kits, plugins, VPN keys) and sessions and settings in ~/.local/state and ~/.config stay"
    else
      note "removed $data_dir, its command links, the PATH block and the Tab completion; sessions and settings in ~/.local/state and ~/.config stay"
    fi
    exit 0 ;;
esac

mkdir -p "$data_dir/versions"
work=$(mktemp -d "$data_dir/.install-XXXXXX")
trap 'rm -rf -- "$work"' EXIT
staged="$work/tree"
signature="" release_version=""
if [[ -n "$from" ]]; then
  if [[ -d "$from" ]]; then
    [[ $verify == 0 ]] || die "--verify checks a bundle (.tar.gz) against its SHA256SUMS.sig, not a directory"
    [[ $policy_signed == 0 ]] || die "the administrator's policy ($POLICY_FILE) requires signed updates:" \
      "install a release, or a bundle with --from BUNDLE.tar.gz --verify"
    mkdir "$staged"
    helper copy "$(cd "$from" && pwd)" "$staged"
  elif [[ -f "$from" ]]; then
    if [[ $verify == 1 || $policy_signed == 1 ]]; then
      # Checked and unpacked from the same copy.
      cp -- "$from" "$work/bundle.tar.gz"
      signature=$(helper verify "$work/bundle.tar.gz" "$from" "${trust[@]}")
      # A release's bundle (its name is in the signed SHA256SUMS): its tree must be that version.
      if [[ $verify == 1 && $(basename -- "$from") =~ ^sandbox-ai-bundle-([0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]+)?)\.tar\.gz$ ]]; then
        release_version=${BASH_REMATCH[1]}
      fi
      note "verified $(basename -- "$from") against SHA256SUMS and its signature by ${signature#good }"
      helper unpack "$work/bundle.tar.gz" "$staged"
    else
      helper unpack "$from" "$staged"
    fi
  else
    die "--from: no such file or directory: $from"
  fi
else
  downloaded=$(helper download "$source_url" "$want_version" "$work/bundle.tar.gz" "$(installed_version)" \
    "$state_file" "$allow_downgrade" "${trust[@]}")
  release_version=${downloaded%%$'\n'*} signature=${downloaded#*$'\n'}
  case "$signature" in
    good*) signed="signed by ${signature#good }" ;;
    unchecked) signed="signature not checked" ;;
    *) signed="not signed" ;;
  esac
  note "downloaded sandbox-ai $release_version from $source_url (SHA-256 verified, $signed)"
  helper unpack "$work/bundle.tar.gz" "$staged"
fi

for required in sandbox/agent_sandbox.py install.sh "${COMMANDS[@]}"; do
  [[ -f "$staged/$required" ]] || die "not a sandbox-ai tree: $required missing"
done
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$staged/sandbox/agent_sandbox.py")
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]+)?$ ]] || die "cannot read the version of the new tree"
if [[ -n "$release_version" && "$version" != "$release_version" ]]; then
  die "the bundle of release $release_version contains sandbox-ai $version; nothing installed"
fi
# The new tree's release keys: only a release a trusted key signed may change them.
[[ -z "$signature" ]] || helper accept "$staged" "$signature" "${trust[@]}"
[[ -z "$release_version" ]] || helper record "$state_file" "$release_version"

current=$(installed_version)
if [[ "$current" == "$version" && $force == 0 && -z "$from" ]]; then
  link_commands
  app_dependencies "$data_dir/current"
  app_desktop_entry
  note "sandbox-ai $version is already installed and up to date"
  shell_rc_setup  # (the block and the completion of this version again: one removed by hand, an older one)
  completion_setup
  end_notes
  running_shell_hint
  exit 0
fi

rm -rf -- "${data_dir:?}/versions/$version"
mv "$staged" "$data_dir/versions/$version"
app_dependencies "$data_dir/versions/$version"
precompile "$data_dir/versions/$version"
chmod -R go-w "$data_dir/versions/$version"
ln -sfn "versions/$version" "$work/current"
# rename(2): the link is replaced at once, never followed (GNU mv -T; macOS' mv has no -T).
python3 -I -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$work/current" "$data_dir/current"
link_commands

# A version a running program still uses (the app, a session's launcher, an MCP server: their command lines
# name the version's folder, which the commands resolve) is kept: it loads more of its modules later, and
# removing them under it ends in "file not found". A later update removes it once nothing uses it.
in_use() {
  local folder=$1 cmdline
  if [[ ! -d /proc/self ]]; then  # macOS: no /proc; ps shows every command line in full (-ww)
    ps -axww -o command= 2>/dev/null | grep -qF -- "$folder/"
    return
  fi
  for cmdline in /proc/[0-9]*/cmdline; do
    [[ -r "$cmdline" ]] || continue
    if tr '\0' '\n' < "$cmdline" 2>/dev/null | grep -qF -- "$folder/"; then return 0; fi
  done
  return 1
}

# Keep the newest few versions for a quick rollback.
# (bash 3.2 and BSD tools as on macOS: no mapfile, no head -n -N, no empty "${old[@]}" under set -u)
old=()
while IFS= read -r line; do old+=("$line"); done < <(ls -1 "$data_dir/versions" | grep -vx "$version" | sort -V |
  awk -v keep=$((KEEP_VERSIONS - 1)) '{ line[NR] = $0 } END { for (i = 1; i <= NR - keep; i++) print line[i] }')
for stale in ${old[@]+"${old[@]}"}; do
  if in_use "$data_dir/versions/$stale"; then
    note "kept sandbox-ai $stale: a running program still uses it (removed at a later update; restart the app or sessions started with it)"
    continue
  fi
  rm -rf -- "${data_dir:?}/versions/$stale"
done

note "installed sandbox-ai $version${current:+ (was $current)} in $data_dir"
note "commands: ${bin_dirs[*]}; check the host with: sandbox-ai doctor; update later with: sandbox-ai update"
app_desktop_entry
if app_linked; then note "the app: sandbox-ai-app (in the desktop menu as \"sandbox-ai\")"; fi
shell_rc_setup
completion_setup
end_notes
running_shell_hint
command -v podman >/dev/null || note "Podman not found; install it (or Bubblewrap and Firejail), then build the images: sandbox-ai build all"
