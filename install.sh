#!/usr/bin/env bash
# sandbox-ai installer: installs or updates to the newest release.
#
# User install (default): ~/.local/share/sandbox-ai, commands linked into
# ~/.local/bin and, if it exists, ~/.bin. System install (root or --system):
# /usr/local/lib/sandbox-ai, commands in /usr/local/bin.
set -euo pipefail

# Where releases come from: a Gitea/Forgejo or GitHub repository URL.
DEFAULT_SOURCE="https://github.com/c0decave/sandbox-ai"
COMMANDS=(sandbox-ai claude-sandbox codex-sandbox opencode-sandbox sandbox-session sandbox-mcp sandbox-wizard sandbox-build-image)
KEEP_VERSIONS=2

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
  --force           Reinstall the same version; replace foreign files in bin directories
  --check           Show the installed and the newest version, change nothing
  --uninstall       Remove the installation and its command links
  -h, --help        This help

Private repositories: put an access token into SANDBOX_AI_TOKEN. It is sent
only to the repository's own host, never printed, and never over plain http
to anything but localhost.
EOF
}

die() { echo "install.sh: $*" >&2; exit 1; }
note() { echo "install.sh: $*" >&2; }

source_url=${SANDBOX_AI_SOURCE:-$DEFAULT_SOURCE}
want_version="" from="" mode="" prefix=/usr/local bin_override="" force=0 action=install
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
    --uninstall) action=uninstall; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done
[[ -z "$want_version" || "$want_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]+)?$ ]] \
  || die "--version expects X.Y.Z"

command -v python3 >/dev/null || die "python3 is required (3.9 or newer)"
python3 -I -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "python3 3.9 or newer is required"

if [[ -z "$mode" ]]; then
  if [[ $(id -u) == 0 ]]; then mode=system; else mode=user; fi
fi
if [[ $mode == system ]]; then
  [[ $(id -u) == 0 || -w "$prefix" ]] || die "--system needs root: sudo bash install.sh --system"
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

# Network, JSON, checksums and safe unpacking: Python's standard library only.
helper() {
  python3 -I - "$@" <<'PY'
import hashlib, ipaddress, json, os, re, shutil, sys, tarfile, urllib.error, urllib.parse, urllib.request

ASSET = re.compile(r"sandbox-ai-bundle-([0-9]+\.[0-9]+\.[0-9]+(?:[.+-][0-9A-Za-z.+-]+)?)\.tar\.gz")


def fail(message):
    sys.exit(f"install.sh: {message}")


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
    """Follow redirects, but never carry the token to another host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
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
        hint = " (private repository? set SANDBOX_AI_TOKEN)" if exc.code in (401, 403, 404) and not token else ""
        raise LookupError(f"{url}: HTTP {exc.code}{hint}") from None
    except (urllib.error.URLError, OSError) as exc:
        fail(f"cannot reach {parts.netloc}: {getattr(exc, 'reason', exc)}")


def release(source, version):
    api, url = api_base(source)
    host = url.netloc if "api.github.com" not in api else "api.github.com"
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
                    fail(f"{source} has no published release")
    except LookupError as exc:
        fail(str(exc))
    assets = {a.get("name"): a.get("browser_download_url") for a in data.get("assets", []) if isinstance(a, dict)}
    bundles = [(m[1], name) for name in assets if isinstance(name, str) and (m := ASSET.fullmatch(name))]
    if not bundles or "SHA256SUMS" not in assets:
        fail(f"release {data.get('tag_name')!r} lacks sandbox-ai-bundle-*.tar.gz or SHA256SUMS")
    found, name = bundles[0]
    return found, name, assets[name], assets["SHA256SUMS"], url.netloc


def cmd_latest(source, version):
    print(release(source, version)[0])


def cmd_download(source, version, target):
    found, name, bundle_url, sums_url, host = release(source, version)
    try:
        sums = fetch(sums_url, host, "application/octet-stream").decode()
        data = fetch(bundle_url, host, "application/octet-stream")
    except LookupError as exc:
        fail(str(exc))
    expected = {parts[1].lstrip("*"): parts[0] for parts in (line.split() for line in sums.splitlines())
                if len(parts) == 2}
    if expected.get(name) != hashlib.sha256(data).hexdigest():
        fail(f"checksum mismatch for {name}; nothing installed")
    with open(target, "wb") as stream:
        stream.write(data)
    print(found)


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
        names = subprocess.run(["git", "-C", checkout, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                               capture_output=True, check=True, text=True).stdout.split("\0")
        names = sorted({n for n in names if n and not n.startswith((".claude/", ".git/"))
                        and os.path.isfile(os.path.join(checkout, n))})
    except (OSError, subprocess.CalledProcessError):
        names = []
    if not names:
        skip = {".git", "__pycache__", ".pytest_cache", "dist", "build", ".claude"}
        for root, dirs, files in os.walk(checkout):
            dirs[:] = [d for d in dirs if d not in skip and not d.endswith(".egg-info")]
            names += [os.path.relpath(os.path.join(root, f), checkout) for f in files]
    for name in names:
        source, path = os.path.join(checkout, name), os.path.join(target, name)
        if os.path.islink(source):
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(source, path)
        os.chmod(path, 0o755 if os.stat(source).st_mode & 0o111 else 0o644)


{"latest": cmd_latest, "download": cmd_download, "unpack": cmd_unpack, "copy": cmd_copy}[sys.argv[1]](*sys.argv[2:])
PY
}

link_commands() {
  local dir cmd target link
  for dir in "${bin_dirs[@]}"; do
    mkdir -p "$dir"
    for cmd in "${COMMANDS[@]}" sandbox-ai-update; do
      link="$dir/$cmd"
      if [[ $cmd == sandbox-ai-update ]]; then target="$data_dir/current/install.sh"; else target="$data_dir/current/$cmd"; fi
      if [[ -e "$link" || -L "$link" ]] && ! { [[ -L "$link" ]] && [[ $(readlink "$link") == "$data_dir"/* ]]; }; then
        if [[ $force == 1 ]]; then
          rm -f "$link"
        else
          note "skipping $link: exists and does not belong to sandbox-ai (use --force to replace)"
          continue
        fi
      fi
      ln -sfn "$target" "$link"
    done
  done
}

path_hint() {
  local dir
  for dir in "${bin_dirs[@]}"; do
    case ":$PATH:" in *":$dir:"*) return 0 ;; esac
  done
  note "add ${bin_dirs[0]} to your PATH, e.g.: echo 'export PATH=\"${bin_dirs[0]}:\$PATH\"' >> ~/.bashrc"
}

case "$action" in
  check)
    current=$(installed_version)
    if [[ -n "$from" ]]; then
      latest="(from $from)"
    else
      latest=$(helper latest "$source_url" "$want_version")
    fi
    echo "installed: ${current:-none} ($data_dir)"
    echo "available: $latest ($source_url)"
    exit 0 ;;
  uninstall)
    for dir in "${bin_dirs[@]}"; do
      for cmd in "${COMMANDS[@]}" sandbox-ai-update; do
        if [[ -L "$dir/$cmd" && $(readlink "$dir/$cmd") == "$data_dir"/* ]]; then rm -f "$dir/$cmd"; fi
      done
    done
    if [[ -d "$data_dir" ]]; then rm -rf -- "$data_dir"; fi
    note "removed $data_dir and its command links; sessions and settings in ~/.local/state and ~/.config stay"
    exit 0 ;;
esac

mkdir -p "$data_dir/versions"
work=$(mktemp -d "$data_dir/.install-XXXXXX")
trap 'rm -rf -- "$work"' EXIT
staged="$work/tree"
if [[ -n "$from" ]]; then
  if [[ -d "$from" ]]; then
    mkdir "$staged"
    helper copy "$(cd "$from" && pwd)" "$staged"
  elif [[ -f "$from" ]]; then
    helper unpack "$from" "$staged"
  else
    die "--from: no such file or directory: $from"
  fi
else
  version=$(helper download "$source_url" "$want_version" "$work/bundle.tar.gz")
  note "downloaded sandbox-ai $version from $source_url (SHA-256 verified)"
  helper unpack "$work/bundle.tar.gz" "$staged"
fi

for required in sandbox/agent_sandbox.py install.sh "${COMMANDS[@]}"; do
  [[ -f "$staged/$required" ]] || die "not a sandbox-ai tree: $required missing"
done
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$staged/sandbox/agent_sandbox.py")
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]+)?$ ]] || die "cannot read the version of the new tree"

current=$(installed_version)
if [[ "$current" == "$version" && $force == 0 && -z "$from" ]]; then
  link_commands
  note "sandbox-ai $version is already installed and up to date"
  path_hint
  exit 0
fi

rm -rf -- "${data_dir:?}/versions/$version"
mv "$staged" "$data_dir/versions/$version"
chmod -R go-w "$data_dir/versions/$version"
ln -sfn "versions/$version" "$work/current"
mv -T "$work/current" "$data_dir/current"
link_commands

# Keep the newest few versions for a quick rollback.
mapfile -t old < <(ls -1 "$data_dir/versions" | grep -vx "$version" | sort -V | head -n -$((KEEP_VERSIONS - 1)))
for stale in "${old[@]}"; do rm -rf -- "${data_dir:?}/versions/$stale"; done

note "installed sandbox-ai $version${current:+ (was $current)} in $data_dir"
note "commands: ${bin_dirs[*]}; check the host with: sandbox-ai doctor; update later with: sandbox-ai update"
path_hint
command -v podman >/dev/null || note "Podman not found; install it (or Bubblewrap and Firejail), then build the images: sandbox-ai build all"
