#!/usr/bin/env python3
"""Updates of the copies install.sh did not install: sandbox-ai.pyz, the AppImage, the macOS app and a pip or pipx
installation replace themselves with the file of a signed release (sandbox-ai update; updates.py calls this).

  the .pyz        sandbox-ai.pyz                           the file it was started from is replaced
  the AppImage    sandbox-ai-ARCH.AppImage                 the file it was started from is replaced
  the macOS app   sandbox-ai-ARCH.dmg                      sandbox-ai.app is replaced with the one in the disk image
  pip, pipx       sandbox_ai_app-X.Y.Z-py3-none-any.whl    installed with the environment's own pip

A release is taken exactly as install.sh takes one: the releases API of the source (GitHub, Gitea, Forgejo), then
SHA256SUMS and SHA256SUMS.sig - the signature must be a good one by a key of this copy's sandbox/release-signers -,
then the file, which must have the SHA-256 the signed SHA256SUMS lists. The release's version is the one of the
bundle that the signed SHA256SUMS lists (a release served under another tag is found out); an older one than the
installed is refused. The new package is started once (--version) before it replaces the old one; the old one is
kept in ${XDG_CACHE_HOME:-~/.cache}/sandbox-ai/update-previous for sandbox-ai update --rollback (pip: the release
before is downloaded and installed again). A copy that cannot be written by whoever runs the update says the command
that can.

SANDBOX_AI_TOKEN: as for install.sh - sent only to the repository's own host, never over plain http but to this
machine, never to where a download redirects. Standard library only (ssh-keygen checks the signature).
"""
import contextlib
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = Path(__file__).resolve().parent
PACKAGES = ("pyz", "appimage", "macos-app")        # PACKAGING beside sandbox/ (main.py's PACKAGES)
FLAVOURS = (*PACKAGES, "pip")                      # the copies this module updates
API_LIMIT, SUMS_LIMIT, SIGNATURE_LIMIT = 16 << 20, 1 << 20, 64 << 10
FILE_LIMIT = 4 << 30                               # bytes of a release file, at most
SELF_TEST_TIMEOUT = 600                            # the new package's first start (it unpacks itself once)
PIP_TIMEOUT = 1800
PENDING, PREVIOUS = "update-pending", "update-previous"
APP_NAME = "sandbox-ai.app"
INSTALL_SH_OPTIONS = {"--user": 0, "--system": 0, "--prefix": 1, "--bin-dir": 1, "--no-modify-rc": 0, "--no-app": 0,
                      "--app-deps": 0, "--no-app-deps": 0, "--app-terminal": 0, "--no-app-terminal": 0,
                      "--no-color": 0, "--color=never": 0, "--color=always": 0, "--color=auto": 0}


class UpdateError(ValueError):
    """An update that did not happen; the message says why and what to do."""


def load(name):
    alias = f"sandbox_ai_{name}_for_package_update"
    loaded = sys.modules.get(alias)
    if loaded is None:
        spec = importlib.util.spec_from_file_location(alias, HERE / f"{name}.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules[alias] = loaded
    return loaded


def printable(text, limit=400):
    text = "".join(c if c.isprintable() else "?" for c in str(text))
    return text if len(text) <= limit else text[:limit] + "…"


def version_key(version):
    match = re.match(r"([0-9]+)\.([0-9]+)\.([0-9]+)(.*)", str(version or ""))
    return (int(match[1]), int(match[2]), int(match[3]), match[4] == "", match[4]) if match else (0, 0, 0, False, "")


def module_version(folder=None):
    """The version of the sandbox-ai whose modules lie in FOLDER (its agent_sandbox.py), or None."""
    try:
        with open(Path(folder or HERE) / "agent_sandbox.py", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                found = re.match(r'__version__ = "([^"\n]{1,40})"$', line.rstrip("\n"))
                if found:
                    return found[1]
    except OSError:
        return None
    return None


# -- this copy --------------------------------------------------------------------------------------------------------

def flavour(info):
    """"pyz", "appimage", "macos-app" or "pip" of an installation INFO (updates.installation), else None."""
    if info.get("kind") == "package" and info.get("package") in PACKAGES:
        return info["package"]
    return "pip" if info.get("kind") == "pip" else None


def app_bundle(tree):
    """sandbox-ai.app around the tree TREE (…/sandbox-ai.app/Contents/Resources/sandbox-ai), or None."""
    tree = Path(tree)
    if len(tree.parents) < 4 or tree.parent.name != "Resources" or tree.parents[1].name != "Contents":
        return None
    bundle = tree.parents[2]
    return bundle if bundle.suffix == ".app" and not bundle.is_symlink() else None


def own_command(info, environ=None):
    """The words that run this copy's command line (before "update")."""
    environ = os.environ if environ is None else environ
    kind, package = flavour(info), environ.get("SANDBOX_AI_PACKAGE") or ""
    if kind == "pyz" and package:
        return [package]
    if kind == "appimage" and package:
        return [package, "cli"]
    if kind == "macos-app":
        bundle = app_bundle(info["tree"])
        if bundle is not None and (bundle / "Contents" / "MacOS" / "sandbox-ai").is_file():
            return [str(bundle / "Contents" / "MacOS" / "sandbox-ai")]
    if kind == "pip":
        script = Path(sys.executable).with_name("sandbox-ai")
        if script.is_file():
            return [str(script)]
    return [sys.executable, "-I", str(HERE / "main.py")]


def as_owner(info, folder, environ=None):
    """What to run when FOLDER is not writable for whoever runs the update (sudo -H: root's own HOME, so that the
    package's start and the update keep their files in root's cache, never in a folder of yours)."""
    return (f"{printable(folder)} is not writable for you; run the update as someone who may write it: sudo -H "
            f"{shlex.join([*own_command(info, environ), 'update'])}")


def writable(path, access=os.access):
    return access(str(path), os.W_OK | os.X_OK)


def target(info, environ=None, access=os.access, machine=None):
    """(PATH, None): the file (the .pyz, the AppImage), the folder (sandbox-ai.app) or the site folder (pip) that
    `sandbox-ai update` replaces for the copy INFO - or (None, WHY): why this copy cannot replace itself, with the
    command that updates it instead. Only files are looked at (no process, no network)."""
    environ = os.environ if environ is None else environ
    kind = flavour(info)
    if kind is None:
        return None, "this copy is not a package or a pip installation"
    assets = load("release_assets")
    if kind in ("appimage", "macos-app") and assets.arch_of(kind, machine) is None:
        import platform
        return None, (f"no {kind} is built for this machine ({printable(machine or platform.machine())}): install "
                      "with install.sh instead, which updates itself")
    if kind in ("pyz", "appimage"):
        package = environ.get("SANDBOX_AI_PACKAGE") or ""
        path = Path(package)
        what = "the .pyz" if kind == "pyz" else "the AppImage"
        if not package or not path.is_absolute():
            return None, (f"this copy was not started from {what} file (its start names the file): start the file "
                          "itself and run its update")
        if path.is_dir():
            return None, (f"this copy runs from an unpacked AppImage ({printable(path)}), not from the AppImage file: "
                          f"download {assets.asset_for(kind, 'X.Y.Z', machine)} from the release page and check it "
                          "with SHA256SUMS")
        if path.is_symlink() or not path.is_file():
            return None, f"{printable(path)}, which this copy was started from, is no longer a file there"
        if not writable(path.parent, access):
            return None, as_owner(info, path.parent, environ)
        return path, None
    if kind == "macos-app":
        bundle = app_bundle(info["tree"])
        if bundle is None:
            return None, (f"{printable(info['tree'])} is not inside a sandbox-ai.app: download the new disk image "
                          "from the release page and check it with SHA256SUMS")
        if str(bundle).startswith("/Volumes/") or "/AppTranslocation/" in str(bundle):
            return None, (f"{APP_NAME} runs from the disk image or from where macOS moved it at its first start "
                          f"({printable(bundle)}): drag it into Applications, start it from there and update then")
        if not writable(bundle.parent, access):
            return None, as_owner(info, bundle.parent, environ)
        return bundle, None
    site = Path(info["tree"])  # pip: the site folder that holds sandbox_ai/
    records = sorted(site.glob("sandbox_ai_app-*.dist-info"))
    if not records:
        old = sorted(site.glob("sandbox_ai-*.dist-info"))
        if old:  # (0.1.94 and before: the package was called sandbox-ai then)
            return None, (f"this is the package of an older name ({old[0].name[:-len('.dist-info')]}): "
                          f"{shlex.quote(sys.executable)} -m pip uninstall sandbox-ai, then "
                          f"{shlex.quote(sys.executable)} -m pip install sandbox-ai-app")
        return None, (f"{printable(site)} holds no pip installation of sandbox-ai-app (no sandbox_ai_app-*.dist-info): "
                      "update it the way it was put there")
    try:
        installer = (records[-1] / "INSTALLER").read_text().strip()
    except (OSError, UnicodeDecodeError):
        installer = "pip"
    if installer == "uv" and importlib.util.find_spec("pip") is None:
        return None, "installed with uv, which keeps no pip in the environment: uv tool upgrade sandbox-ai-app " \
                     "(or: uv pip install --upgrade sandbox-ai-app)"
    if importlib.util.find_spec("pip") is None:
        return None, (f"this Python environment has no pip ({printable(sys.executable)} -m pip): update the package "
                      "the way it was installed (pipx upgrade sandbox-ai-app)")
    if not writable(site, access) or not writable(site / "sandbox_ai", access):
        return None, as_owner(info, site, environ)
    return site, None


def cache_folder(name, kind, environ=None, create=True):
    """${XDG_CACHE_HOME:-~/.cache}/sandbox-ai/NAME/KIND, private (0700), never through a link."""
    environ = os.environ if environ is None else environ
    base = environ.get("XDG_CACHE_HOME") or ""
    if not os.path.isabs(base):
        base = os.path.join(environ.get("HOME") or os.path.expanduser("~"), ".cache")
    path = Path(base)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for part in ("sandbox-ai", name, kind):
        path = path / part
        if create:
            with contextlib.suppress(FileExistsError):
                os.mkdir(path, 0o700)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            if create:
                raise
            continue
        if not os.path.isdir(path) or path.is_symlink() or info.st_uid != os.geteuid():
            raise UpdateError(f"{path} is a link, not a folder or not yours; not used" + (
                " (as root: sudo -H, so that root's own cache is used)" if os.geteuid() == 0 and
                not path.is_symlink() and os.path.isdir(path) else ". Remove it and try again"))
        if create and part != "sandbox-ai" and info.st_mode & 0o077:
            os.chmod(path, 0o700)
    return path


def pending_folder(info, environ=None, create=True):
    return cache_folder(PENDING, flavour(info), environ, create)


def previous_folder(info, environ=None, create=True):
    return cache_folder(PREVIOUS, flavour(info), environ, create)


# -- the release source -----------------------------------------------------------------------------------------------

def api_base(source):
    """(API base, the repository URL's parts, GitHub?) of a repository URL, as install.sh's helper."""
    url = urllib.parse.urlsplit(re.sub(r"\.git$", "", str(source).rstrip("/")))
    parts = [p for p in url.path.split("/") if p]
    if url.scheme not in ("http", "https") or len(parts) < 2 or not url.hostname:
        raise UpdateError(f"the release source must be http(s)://HOST/OWNER/REPO: {printable(source)!r}")
    owner, repo, root = parts[-2], parts[-1], "/".join(parts[:-2])
    if url.hostname in ("github.com", "www.github.com"):
        return f"https://api.github.com/repos/{owner}/{repo}", url, True
    base = f"{url.scheme}://{url.netloc}" + (f"/{root}" if root else "")
    return f"{base}/api/v1/repos/{owner}/{repo}", url, False


def loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry the token to another host, nor over plain http but to this machine."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        target_url, origin = urllib.parse.urlsplit(newurl), urllib.parse.urlsplit(req.full_url)
        if new is not None and target_url.scheme not in ("http", "https"):
            raise UpdateError(f"{origin.netloc} redirects to {printable(newurl, 120)}: not followed")
        if new is not None and (target_url.netloc != origin.netloc
                                or (target_url.scheme != "https" and not loopback(target_url.hostname or ""))):
            new.remove_header("Authorization")
        return new


class NotFound(LookupError):
    pass


def open_url(url, token_host, accept="application/json", environ=None, timeout=60):
    """The open response of URL. SANDBOX_AI_TOKEN goes along only when URL's host is TOKEN_HOST."""
    environ = os.environ if environ is None else environ
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "sandbox-ai-update"})
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UpdateError(f"not an http(s) address: {printable(url, 120)}")
    token = environ.get("SANDBOX_AI_TOKEN")
    if token and parts.netloc == token_host:
        if parts.scheme != "https" and not loopback(parts.hostname or ""):
            raise UpdateError(f"refusing to send SANDBOX_AI_TOKEN over plain http to {parts.hostname}")
        if not re.fullmatch(r"[A-Za-z0-9_.~+/=-]{8,512}", token):
            raise UpdateError("SANDBOX_AI_TOKEN does not look like an access token")
        request.add_header("Authorization", f"token {token}")
    opener = urllib.request.build_opener(SameHostRedirect)
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        limit, retry = exc.headers.get("X-RateLimit-Remaining"), exc.headers.get("Retry-After", "")
        if exc.code in (403, 429) and (limit == "0" or retry):
            reset = exc.headers.get("X-RateLimit-Reset", "")
            when = (f"after {time.strftime('%H:%M', time.localtime(int(reset)))}" if reset.isdigit()
                    else f"in {retry} seconds" if retry.isdigit() else "later")
            hint = "" if token else ("; GitHub allows 60 API requests an hour per address without a token: "
                                     "SANDBOX_AI_TOKEN with any token (no scopes needed for a public repository) "
                                     "raises that to 5000")
            raise UpdateError(f"{parts.netloc} limits the requests to its API (HTTP {exc.code}); try again "
                              f"{when}{hint}") from None
        hint = " (private repository? set SANDBOX_AI_TOKEN)" if exc.code in (401, 403, 404) and not token else ""
        raise NotFound(f"{printable(url, 200)}: HTTP {exc.code}{hint}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(f"cannot reach {parts.netloc}: {printable(getattr(exc, 'reason', exc), 200)}") from None


def fetch(url, token_host, accept="application/json", limit=API_LIMIT, environ=None):
    """The bytes of URL, LIMIT at most (more: an error)."""
    try:
        with open_url(url, token_host, accept, environ) as response:
            data = response.read(limit + 1)
    except NotFound as exc:
        raise UpdateError(str(exc)) from None
    except OSError as exc:
        raise UpdateError(f"reading {printable(url, 200)} failed: {printable(exc, 200)}") from None
    if len(data) > limit:
        raise UpdateError(f"{printable(url, 200)} is larger than {limit} bytes: not taken")
    return data


def download(url, token_host, path, limit=FILE_LIMIT, environ=None):
    """URL into the new file PATH (0600), in pieces: its SHA-256. LIMIT bytes at most."""
    digest, size = hashlib.sha256(), 0
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream, open_url(url, token_host, "application/octet-stream", environ) as response:
            while True:
                block = response.read(1 << 20)
                if not block:
                    break
                size += len(block)
                if size > limit:
                    raise UpdateError(f"{printable(url, 200)} is larger than {limit} bytes: not taken")
                digest.update(block)
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
    except NotFound as exc:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise UpdateError(str(exc)) from None
    except (OSError, UpdateError) as exc:
        with contextlib.suppress(OSError):
            os.unlink(path)
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(f"the download of {printable(url, 200)} failed: {printable(exc, 200)}") from None
    return digest.hexdigest()


def release(source, version=None, environ=None):
    """{"version", "tag", "assets": {name: (url, token host)}, "source"} of the newest release of SOURCE (or of
    VERSION): the version is the one in the name of its bundle, as install.sh reads it."""
    environ = os.environ if environ is None else environ
    assets_module = load("release_assets")
    api, url, github = api_base(source)
    host = "api.github.com" if github else url.netloc
    try:
        if version:
            try:
                data = json.loads(fetch(f"{api}/releases/tags/v{version}", host, environ=environ))
            except UpdateError as exc:
                if "HTTP 404" not in str(exc):
                    raise
                # (a version never published there - one installed from a checkout, a rollback to it -, or removed)
                raise UpdateError(f"{printable(source)} has no release {version} (HTTP 404: never published there, "
                                  "or removed" + ("" if environ.get("SANDBOX_AI_TOKEN") else
                                                  "; a private repository needs SANDBOX_AI_TOKEN") + ")") from None
        else:
            try:
                with open_url(f"{api}/releases/latest", host, environ=environ) as response:
                    data = json.loads(response.read(API_LIMIT))
            except NotFound:  # (older Gitea: the newest published one of the list)
                listing = json.loads(fetch(f"{api}/releases?limit=20", host, environ=environ))
                data = next((r for r in listing if isinstance(r, dict) and not r.get("draft")
                             and not r.get("prerelease")), None) if isinstance(listing, list) else None
                if data is None:
                    raise UpdateError(f"{printable(source)} has no published release yet") from None
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError(f"{printable(source)}: the answer of its releases API is not readable") from None
    if not isinstance(data, dict):
        raise UpdateError(f"{printable(source)}: the answer of its releases API is not a release")
    token = environ.get("SANDBOX_AI_TOKEN")
    found = {}
    for asset in data.get("assets") or []:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            continue
        # A private GitHub repository serves files to a token only through the API; public ones use the download
        # link, which costs no API request.
        api_url = asset.get("url")
        if github and token and isinstance(api_url, str) and api_url.startswith(f"{api}/releases/assets/"):
            found[asset["name"]] = (api_url, "api.github.com")
        elif isinstance(asset.get("browser_download_url"), str):
            found[asset["name"]] = (asset["browser_download_url"], url.netloc)
    bundles = [match[1] for name in found if (match := assets_module.BUNDLE.fullmatch(name))]
    if not bundles or assets_module.SUMS not in found:
        raise UpdateError(f"release {printable(data.get('tag_name'), 60)!r} of {printable(source)} lacks "
                          "sandbox-ai-bundle-*.tar.gz or SHA256SUMS")
    return {"version": bundles[0], "tag": data.get("tag_name"), "assets": found, "source": str(source)}


def policy_requires_signatures():
    """Whether the administrator's policy demands signed updates (/etc/sandbox-ai/policy.json)."""
    try:
        return bool(load("agent_sandbox").load_policy().get("require_signed_updates", False))
    except SystemExit as exc:
        raise UpdateError(f"the administrator's policy is not readable ({printable(exc.code)}): nothing updated") \
            from None


def trusted_keys():
    keys = load("release_keys")
    try:
        return keys, keys.load()
    except ValueError as exc:
        raise UpdateError(str(exc)) from None


def judge(sums, signature, what):
    """The signature status of SHA256SUMS ("good FINGERPRINT", "unsigned": no key is trusted yet), or UpdateError:
    once this copy lists release keys, one of them must have signed."""
    keys, trusted = trusted_keys()
    if not trusted:
        if policy_requires_signatures():
            raise UpdateError("the administrator's policy requires signed updates (require_signed_updates), but this "
                              "copy trusts no release key; nothing updated")
        return "unsigned"
    listed = ", ".join(keys.fingerprint(key) for _, key in trusted)
    if signature is None:
        raise UpdateError(f"{what} is not signed (no SHA256SUMS.sig); nothing updated. Trusted release keys: {listed}")
    try:
        verdict, detail = keys.verify(sums, signature, trusted)
    except RuntimeError as exc:
        raise UpdateError(str(exc)) from None
    if verdict != "good":
        raise UpdateError(f"{detail}; nothing updated. Trusted release keys: {listed}")
    return f"good {detail}"


def signed_version(sums, claimed, what):
    """{name: sha256} of the SHA256SUMS text SUMS, which must be the list of release CLAIMED (its bundle's name)."""
    assets = load("release_assets")
    listed = assets.checksums(sums.decode("utf-8", errors="replace"))
    version = assets.bundle_version(listed)
    if version is None or (claimed is not None and version != claimed):
        raise UpdateError(f"SHA256SUMS of {what} lists " + (f"the files of sandbox-ai {version}" if version else
                          "no sandbox-ai bundle") + f", not those of {claimed}: nothing updated")
    return listed, version


def source_key(source):
    return re.sub(r"\.git$", "", str(source or "").rstrip("/"))


def seen_file(environ=None, create=False):
    return cache_folder("update-seen", "releases", environ, create=create) / "seen.json"


def seen_at(source, environ=None):
    """The newest release of SOURCE this user's copies have seen signed by a trusted key (a check, an update), or
    None: an older newest from the same source afterwards may be an old release served again."""
    try:
        value = json.loads(seen_file(environ).read_text()).get(source_key(source))
    except (UpdateError, OSError, ValueError, AttributeError, RecursionError):
        return None
    return value if isinstance(value, str) and re.fullmatch(load("release_assets").VERSION, value) else None


def record_seen(source, version, environ=None):
    """Remember VERSION as seen signed at SOURCE (when newer than what was seen there). Never an error."""
    with contextlib.suppress(UpdateError, OSError, ValueError, RecursionError):
        known = seen_at(source, environ)
        if known and version_key(known) >= version_key(version):
            return
        path = seen_file(environ, create=True)
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError, RecursionError):
            document = {}
        document = document if isinstance(document, dict) else {}
        document[source_key(source)] = version
        temporary = path.with_name(f".seen.json.{os.getpid()}")
        temporary.write_text(json.dumps(document, indent=1))
        os.replace(temporary, path)


def check(source, environ=None):
    """{"available", "signature" ("good", "bad", "none", "unchecked"), "signer", "error"} of the newest release of
    SOURCE, its signature checked with this copy's release keys (what updates.check records)."""
    nothing = {"available": None, "signature": None, "signer": None, "error": None}
    try:
        found = release(source, None, environ)
        host_sums = found["assets"][load("release_assets").SUMS]
        sums = fetch(*host_sums, accept="application/octet-stream", limit=SUMS_LIMIT, environ=environ)
        signature_asset = found["assets"].get(load("release_assets").SIGNATURE)
        if signature_asset is None:
            return {**nothing, "available": found["version"], "signature": "none"}
        signature = fetch(*signature_asset, accept="application/octet-stream", limit=SIGNATURE_LIMIT, environ=environ)
        keys, trusted = trusted_keys()
        if not trusted:
            return {**nothing, "available": found["version"], "signature": "unchecked"}
        verdict, detail = keys.verify(sums, signature, trusted)
        if verdict == "good":
            try:
                signed_version(sums, found["version"], f"release {found['version']}")
            except UpdateError as exc:
                return {**nothing, "available": found["version"], "signature": "bad", "error": str(exc)}
            record_seen(source, found["version"], environ)
        return {**nothing, "available": found["version"], "signature": verdict,
                "signer": detail if verdict == "good" else None}
    except (UpdateError, RuntimeError, OSError) as exc:
        return {**nothing, "error": printable(exc)}


def fetch_release(info, source, version=None, allow_downgrade=False, force=False, environ=None, machine=None):
    """The release file for this copy (INFO), downloaded into its pending folder and checked in full - SHA256SUMS
    signed by a trusted key, the file as listed there -, with SHA256SUMS and SHA256SUMS.sig as they came:
    {"version", "file", "signature", "name"}. Nothing is installed."""
    assets = load("release_assets")
    kind = flavour(info)
    found = release(source, version, environ)
    what = f"the release {found['version']}"
    sums = fetch(*found["assets"][assets.SUMS], accept="application/octet-stream", limit=SUMS_LIMIT, environ=environ)
    signature_asset = found["assets"].get(assets.SIGNATURE)
    signature = fetch(*signature_asset, accept="application/octet-stream", limit=SIGNATURE_LIMIT, environ=environ) \
        if signature_asset else None
    status = judge(sums, signature, what)
    listed, release_version = signed_version(sums, found["version"], what)
    installed = current_version(info, environ, machine)
    seen = seen_at(source, environ)
    if status.startswith("good"):
        record_seen(source, release_version, environ)
    if not version and not allow_downgrade and version_key(release_version) < version_key(installed) and \
            not (seen and version_key(seen) > version_key(release_version)):
        # (newer than the newest release - a copy from a checkout, another source, a release not published there
        # yet: nothing to update; after a newer release of this source was seen, an older newest is refused)
        raise UpToDate(installed, release_version)
    refuse_older(release_version, installed, allow_downgrade, explicit=bool(version))
    if release_version == installed and not force:
        raise UpToDate(installed)
    name = assets.asset_for(kind, release_version, machine)
    if name is None or name not in found["assets"] or name not in listed:
        page = f"{str(source).rstrip('/')}/releases/tag/v{release_version}"
        raise UpdateError(f"{what} has no {name or 'file for this machine'}" + (
            " in its SHA256SUMS" if name and name in found["assets"] else "") + f": nothing updated (see {page})")
    drop_pending(info, environ)
    folder = pending_folder(info, environ)
    path = folder / name
    digest = download(*found["assets"][name], path, environ=environ)
    if digest != listed[name]:
        drop_pending(info, environ)
        raise UpdateError(f"checksum mismatch for {name}; nothing updated")
    (folder / assets.SUMS).write_bytes(sums)
    if signature is not None:
        (folder / assets.SIGNATURE).write_bytes(signature)
    return {"version": release_version, "file": str(path), "signature": status, "name": name}


class UpToDate(UpdateError):
    """Nothing to update: VERSION is installed - the newest release, or newer than it (NEWEST: the newest release,
    older than the installed one: installed from a checkout, another source, a release not published here yet)."""
    def __init__(self, version, newest=None):
        super().__init__(f"sandbox-ai {version} is newer than the newest release {newest}: nothing to update (an "
                         "older release is installed only with --allow-downgrade)" if newest else
                         f"sandbox-ai {version} is already installed and up to date")
        self.version, self.newest = version, newest


def refuse_older(found, installed, allow_downgrade=False, explicit=False):
    if version_key(found) < version_key(installed) and not allow_downgrade:
        why = "" if explicit else (", although the source lists it as the newest release and a newer one was seen "
                                   "there before (an old release served again?)")
        raise UpdateError(f"sandbox-ai {found} is older than the installed {installed}{why}; nothing updated. "
                          "--allow-downgrade installs it anyway")


def drop_pending(info, environ=None):
    with contextlib.suppress(UpdateError, OSError):
        folder = pending_folder(info, environ, create=False)
        if folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder, ignore_errors=True)


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def pending(info, environ=None, machine=None):
    """{"version", "file", "name"} of what fetch_release left for this copy, by its names only (verified_pending
    checks it), or None."""
    assets = load("release_assets")
    try:
        folder = pending_folder(info, environ, create=False)
        sums = (folder / assets.SUMS).read_bytes()
    except (UpdateError, OSError):
        return None
    listed = assets.checksums(sums.decode("utf-8", errors="replace"))
    version = assets.bundle_version(listed)
    name = assets.asset_for(flavour(info), version, machine) if version else None
    path = folder / name if name else None
    if path is None or path.is_symlink() or not path.is_file():
        return None
    return {"version": version, "file": str(path), "name": name}


def verified_pending(info, environ=None, machine=None):
    """pending(), checked again as a download is: the signature of its SHA256SUMS by a trusted key, the file's
    SHA-256 as listed. {"version", "file", "name", "signature"}; UpdateError otherwise (and the folder is removed)."""
    assets = load("release_assets")
    found = pending(info, environ, machine)
    if found is None:
        raise UpdateError("nothing is downloaded (update mode download or auto fetches a new release; sandbox-ai "
                          "update installs the newest now)")
    folder = Path(found["file"]).parent
    sums = (folder / assets.SUMS).read_bytes()
    try:
        signature = (folder / assets.SIGNATURE).read_bytes()
    except OSError:
        signature = None
    try:
        status = judge(sums, signature, f"the downloaded {found['name']}")
        listed, _ = signed_version(sums, found["version"], f"the downloaded {found['name']}")
        if sha256_of(found["file"]) != listed.get(found["name"]):
            raise UpdateError(f"checksum mismatch for the downloaded {found['name']}; nothing updated")
    except UpdateError:
        drop_pending(info, environ)
        raise
    return {**found, "signature": status}


# -- replacing --------------------------------------------------------------------------------------------------------

def clean_environment(environ=None):
    """The environment of a start of the new package: nothing of the package that runs now."""
    environ = dict(os.environ if environ is None else environ)
    for name in ("APPIMAGE", "APPDIR", "ARGV0", "OWD", "SANDBOX_AI_PACKAGE", "SANDBOX_AI_PACKAGE_COPY",
                 "SANDBOX_AI_HOME", "SANDBOX_AI_APP_PACKAGE_LAUNCHER", "SANDBOX_AI_APP_LAUNCHER"):
        environ.pop(name, None)
    return environ


def started_version(command, runner=subprocess.run, environ=None):
    """What `COMMAND --version` says its version is; UpdateError when it does not start."""
    try:
        done = runner([*command, "--version"], capture_output=True, text=True, timeout=SELF_TEST_TIMEOUT,
                      stdin=subprocess.DEVNULL, env=clean_environment(environ))
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"the new package does not start ({printable(exc, 300)}); nothing replaced") from None
    found = re.fullmatch(r"sandbox-ai (\S+)", (done.stdout or "").strip())
    if done.returncode != 0 or not found:
        lines = [line for line in ((done.stderr or "") + (done.stdout or "")).splitlines() if line.strip()]
        raise UpdateError(f"the new package does not start (exit status {done.returncode}"
                          + (f": {printable(lines[-1], 300)}" if lines else "") + "); nothing replaced")
    return found[1]


def appimage_version(path, runner=subprocess.run, environ=None):
    """started_version of the AppImage at PATH: mounted as usual, else - no FUSE here, as in most containers -
    unpacked and run (--appimage-extract-and-run) in a private temporary folder."""
    try:
        return started_version([str(path), "cli"], runner, environ)
    except UpdateError as first:
        with tempfile.TemporaryDirectory(prefix="sandbox-ai-appimage.") as private:
            try:
                return started_version([str(path), "--appimage-extract-and-run", "cli"], runner,
                                       {**(os.environ if environ is None else environ), "TMPDIR": private})
            except UpdateError:
                raise first from None


def identity(path):
    """What tells the file PATH from another put in its place: [size, time of change in ns, inode]; None: no file."""
    try:
        found = os.stat(path)
    except OSError:
        return None
    return [found.st_size, found.st_mtime_ns, found.st_ino] if os.path.isfile(path) else None


def record_previous(info, version, target_path, kept, now, environ=None):
    """previous.json: the version kept (VERSION, in the file or folder KEPT) and what is in place now - NOW, with
    the file's identity: a copy of the package that still runs (the app, after its own update) is an older one than
    the file it was started from, and says so (current_version)."""
    folder = previous_folder(info, environ)
    document = {"version": version, "target": str(target_path), "kept": kept, "time": time.time(),
                "now": {"version": now, "file": identity(target_path)}}
    temporary = folder / f".previous.json.{os.getpid()}"
    temporary.write_text(json.dumps(document, indent=1))
    os.replace(temporary, folder / "previous.json")


def current_version(info, environ=None, machine=None):
    """The version `sandbox-ai` of this copy is at its next start: this copy's own - or, for the .pyz and the
    AppImage, the one of the file in place when an update or a rollback of ours put it there and this copy (unpacked
    from the file before) still runs: the app after Install now, until it is restarted."""
    own = module_version() or "unknown"
    if flavour(info) not in ("pyz", "appimage"):
        return own
    path, _ = target(info, environ, machine=machine)
    try:
        document = json.loads((previous_folder(info, environ, create=False) / "previous.json").read_text())
        now = document["now"]
        if path is not None and document.get("target") == str(path) and now["file"] == identity(path) \
                and re.fullmatch(load("release_assets").VERSION, now["version"]):
            return now["version"]
    except (UpdateError, OSError, ValueError, KeyError, TypeError, RecursionError):
        pass
    return own


def previous(info, environ=None):
    """{"version", "target", "kept"} of the version kept before the last update of this copy, or None: the kept
    file or folder must still be there and belong to the place this copy is updated in."""
    try:
        folder = previous_folder(info, environ, create=False)
        document = json.loads((folder / "previous.json").read_text())
    except (UpdateError, OSError, ValueError, RecursionError):
        return None
    if not isinstance(document, dict) or not isinstance(document.get("version"), str) \
            or not re.fullmatch(load("release_assets").VERSION, document["version"]):
        return None
    kind = flavour(info)
    if kind == "pip":
        return document if document.get("target") == str(info.get("tree")) else None
    kept = folder / str(document.get("kept") or "")
    if not document.get("kept") or "/" in str(document["kept"]) or kept.is_symlink() or not kept.exists():
        return None
    return {**document, "path": str(kept)}


def move(source, destination):
    """SOURCE to DESTINATION (which must not exist): a rename, or - another file system - a copy, then the removal."""
    try:
        os.rename(source, destination)
    except OSError:
        if os.path.isdir(source) and not os.path.islink(source):
            shutil.copytree(source, destination, symlinks=True)
            shutil.rmtree(source)
        else:
            shutil.copy2(source, destination)
            os.unlink(source)


def remove(path):
    path = Path(path)
    if path.is_symlink() or path.is_file():
        with contextlib.suppress(OSError):
            path.unlink()
    elif path.is_dir():
        subprocess.run(["chmod", "-R", "u+w", "--", str(path)], capture_output=True, check=False)
        shutil.rmtree(path, ignore_errors=True)


def staged_copy(source, folder, name, mode=0o755):
    """SOURCE copied into FOLDER as a hidden file beside NAME (on its file system: the rename into place is atomic)."""
    fd, staged = tempfile.mkstemp(prefix=f".{name}.update-", dir=folder)
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as stream:
            shutil.copyfileobj(stream, out, 1 << 20)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(staged, mode)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(staged)
        raise
    return Path(staged)


def keep_file(info, path, environ=None):
    """The file PATH (the version in place now) kept for --rollback: a hard link in the cache where that works,
    else a copy. Its name there."""
    folder = previous_folder(info, environ)
    for old in folder.iterdir():
        remove(old)
    kept = folder / Path(path).name
    try:
        os.link(path, kept)
    except OSError:
        shutil.copy2(path, kept)
    return kept.name


def replace_file(info, new, version, path, runner=subprocess.run, environ=None):
    """The .pyz or the AppImage at PATH replaced with the file NEW of VERSION: staged beside it, started once
    (--version must say VERSION), the old one kept, then renamed into place (a program that runs from the old file
    keeps it: its inode stays until it ends)."""
    kind, installed = flavour(info), current_version(info, environ)
    staged = staged_copy(new, path.parent, path.name, 0o755 if os.access(path, os.X_OK) else 0o644)
    try:
        if kind == "pyz":
            said = started_version([sys.executable, "-I", str(staged)], runner, environ)
        else:
            os.chmod(staged, 0o755)
            said = appimage_version(staged, runner, environ)
        if said != version:
            raise UpdateError(f"the file of release {version} says it is sandbox-ai {printable(said, 40)}; nothing "
                              "replaced")
        kept = keep_file(info, path, environ)
        os.replace(staged, path)
        record_previous(info, installed, path, kept, version, environ)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(staged)
    return installed, version


def macos_tools(which=shutil.which):
    hdiutil = which("hdiutil")
    if not hdiutil:
        raise UpdateError("replacing sandbox-ai.app needs hdiutil (part of macOS): open the downloaded disk image and "
                          "drag sandbox-ai.app into Applications yourself")
    return hdiutil, which("ditto")


def replace_app(info, image, version, bundle, runner=subprocess.run, environ=None, which=shutil.which):
    """sandbox-ai.app at BUNDLE replaced with the one in the disk image IMAGE of VERSION: attached read-only, copied
    beside BUNDLE (ditto keeps its signature), checked (the tree's version, a start of its command line), the old
    app moved into the cache for --rollback, the new one renamed into place."""
    installed = module_version() or "unknown"
    hdiutil, ditto = macos_tools(which)
    mount = Path(tempfile.mkdtemp(prefix="mount-", dir=pending_folder(info, environ)))
    staged = Path(tempfile.mkdtemp(prefix=f".{bundle.name}.update-", dir=bundle.parent))
    attached = False
    try:
        done = runner([hdiutil, "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount),
                       str(image)], capture_output=True, text=True, timeout=600, stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            raise UpdateError(f"hdiutil could not open {Path(image).name} "
                              f"({printable((done.stderr or done.stdout).strip()[-300:])}); nothing replaced")
        attached = True
        source = mount / APP_NAME
        tree = source / "Contents" / "Resources" / "sandbox-ai"
        if source.is_symlink() or not source.is_dir() or module_version(tree / "sandbox") != version:
            raise UpdateError(f"{Path(image).name} holds no {APP_NAME} of sandbox-ai {version}; nothing replaced")
        new = staged / bundle.name
        copy = runner([ditto, str(source), str(new)] if ditto else ["cp", "-R", str(source), str(new)],
                      capture_output=True, text=True, timeout=1800, stdin=subprocess.DEVNULL)
        if copy.returncode != 0 or not new.is_dir():
            raise UpdateError(f"copying {APP_NAME} out of the disk image failed "
                              f"({printable((copy.stderr or copy.stdout).strip()[-300:])}); nothing replaced")
        said = started_version([str(new / "Contents" / "MacOS" / "sandbox-ai")], runner, environ)
        if said != version:
            raise UpdateError(f"the app of release {version} says it is sandbox-ai {printable(said, 40)}; nothing "
                              "replaced")
        folder = previous_folder(info, environ)
        for old in folder.iterdir():
            remove(old)
        kept = folder / bundle.name
        move(str(bundle), str(kept))
        try:
            os.rename(new, bundle)
        except OSError as exc:
            move(str(kept), str(bundle))  # (back as it was)
            raise UpdateError(f"the new app could not be put into place ({printable(exc)}); nothing replaced") \
                from None
        record_previous(info, installed, bundle, kept.name, version, environ)
    finally:
        if attached:
            runner([hdiutil, "detach", "-quiet", str(mount)], capture_output=True, text=True, timeout=300,
                   stdin=subprocess.DEVNULL)
        remove(staged)
        with contextlib.suppress(OSError):
            mount.rmdir()
    return installed, version


def pip_command(site, wheel):
    """(argv, cwd-free environment changes) of the pip that installs WHEEL into the environment whose site folder is
    SITE: the environment's own pip, isolated (-I; -E for a --user installation, whose folder -I would hide), with
    the extra [gui] when the app's PySide6 is installed there (its pins may move with the release)."""
    import site as site_module
    user = False
    with contextlib.suppress(Exception):
        user = os.path.realpath(site_module.getusersitepackages()) == os.path.realpath(site)
    gui = any(True for pattern in ("PySide6_Essentials-*.dist-info", "pyside6_essentials-*.dist-info")
              for _ in Path(site).glob(pattern))
    command = [sys.executable, "-E" if user else "-I", "-m", "pip", "install", "--disable-pip-version-check",
               "--no-input", "--upgrade", "--force-reinstall", "--no-deps"]
    if user:
        command.append("--user")
    command.append(str(wheel))
    # The app's PySide6: only what the new release pins differently is fetched (a second run, with dependencies).
    extras = [sys.executable, "-E" if user else "-I", "-m", "pip", "install", "--disable-pip-version-check",
              "--no-input", *(["--user"] if user else []), f"{wheel}[gui]"] if gui else None
    return command, extras


def replace_pip(info, wheel, version, site, runner=subprocess.run, environ=None, extras=True):
    """The wheel of VERSION installed with the environment's own pip (never from an index: the file that was
    checked); the app's PySide6 follows the release's pins when it is installed there - EXTRAS False (the update
    timer's own install): left as it is and said, since nothing is fetched from PyPI unasked."""
    installed = module_version() or "unknown"
    command, gui = pip_command(site, wheel)
    env = {key: value for key, value in (os.environ if environ is None else environ).items()
           if key not in ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONUSERBASE")}
    with tempfile.TemporaryDirectory(prefix="sandbox-ai-pip.") as empty:  # (python -m looks into the current folder)
        try:
            done = runner(command, capture_output=True, text=True, timeout=PIP_TIMEOUT, stdin=subprocess.DEVNULL,
                          env=env, cwd=empty)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UpdateError(f"pip could not be run ({printable(exc, 300)}); nothing updated") from None
        if done.returncode != 0:
            said = [line for line in (done.stderr or done.stdout or "").splitlines() if line.strip()]
            hint = ""
            if any("externally-managed-environment" in line for line in said):
                hint = (" (this Python is managed by the system: install sandbox-ai-app with pipx or in a virtual "
                        "environment, or use install.sh)")
            raise UpdateError(f"pip install of {Path(wheel).name} failed"
                              + (f": {printable(said[-1], 300)}" if said else "") + hint)
        after = module_version(Path(site) / "sandbox_ai")
        if after != version:
            raise UpdateError(f"pip installed {Path(wheel).name}, but {site} holds sandbox-ai {after}")
        note = None
        pins = shlex.join([sys.executable, "-m", "pip", "install", f"{wheel}[gui]"])
        if gui and not extras:
            note = (f"the app's PySide6 was left as it is (an update by the timer fetches nothing from PyPI); if the "
                    f"app does not start with this release, bring it to its pins: {pins}")
        elif gui:
            more = runner(gui, capture_output=True, text=True, timeout=PIP_TIMEOUT, stdin=subprocess.DEVNULL,
                          env=env, cwd=empty)
            if more.returncode != 0:
                note = (f"the app's PySide6 was left as it is (pip could not bring it to this release's pins: "
                        f"PyPI not reachable?); later: {pins}")
    folder = previous_folder(info, environ)
    for old in folder.iterdir():
        remove(old)
    record_previous(info, installed, site, None, version, environ)
    return installed, version, note


def install(info, found, runner=subprocess.run, environ=None, machine=None, scheduled=False):
    """The checked release file FOUND ({"version", "file"}: verified_pending, fetch_release) put in the place of this
    copy: (old version, new version, a note or None). SCHEDULED: by the update timer (nothing but the release's file
    is fetched)."""
    path, why = target(info, environ, machine=machine)
    if path is None:
        raise UpdateError(f"this copy does not replace itself: {why}")
    kind = flavour(info)
    if kind in ("pyz", "appimage"):
        return (*replace_file(info, found["file"], found["version"], path, runner, environ), None)
    if kind == "macos-app":
        return (*replace_app(info, found["file"], found["version"], path, runner, environ), None)
    return replace_pip(info, found["file"], found["version"], path, runner, environ, extras=not scheduled)


def rollback(info, source=None, runner=subprocess.run, environ=None, machine=None):
    """Back to the version kept before the last update: the kept file or app swapped with the one in place (so the
    version left can be taken again); pip: the release before downloaded - signed, checked - and installed.
    (from, to)."""
    path, why = target(info, environ, machine=machine)
    if path is None:
        raise UpdateError(f"this copy does not replace itself: {why}")
    kept = previous(info, environ)
    if kept is None or (flavour(info) != "pip" and kept.get("target") != str(path)):
        raise UpdateError("no version is kept to go back to (the one before an update is kept in "
                          f"{previous_folder(info, environ, create=False)})")
    current = current_version(info, environ, machine)
    kind = flavour(info)
    if kind == "pip":
        found = fetch_release(info, source, kept["version"], allow_downgrade=True, force=True, environ=environ,
                              machine=machine)
        try:
            replace_pip(info, found["file"], found["version"], path, runner, environ)
        finally:
            drop_pending(info, environ)
        return current, kept["version"]
    folder = previous_folder(info, environ)
    aside = folder / f".swap-{os.getpid()}"
    remove(aside)
    if kind == "macos-app":
        move(str(path), str(aside))
        try:
            move(kept["path"], str(path))
        except OSError as exc:
            move(str(aside), str(path))
            raise UpdateError(f"the app kept before could not be put back ({printable(exc)})") from None
        move(str(aside), kept["path"])
    else:
        staged = staged_copy(kept["path"], path.parent, path.name, 0o755 if os.access(path, os.X_OK) else 0o644)
        try:
            try:
                os.link(path, aside)
            except OSError:
                shutil.copy2(path, aside)
            os.replace(staged, path)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(staged)
        os.replace(aside, kept["path"])
    record_previous(info, current, path, Path(kept["path"]).name, kept["version"], environ)
    return current, kept["version"]


def timer_command(info, environ=None):
    """What the update timer runs for this copy, by a path that an update does not move: the package file itself
    (the copy it unpacks lies in a folder named after its version), the app's own command line, the environment's
    Python with this module's main.py."""
    environ = os.environ if environ is None else environ
    kind, package = flavour(info), environ.get("SANDBOX_AI_PACKAGE") or ""
    if kind == "pyz" and os.path.isfile(package):
        python = shutil.which("python3") or sys.executable
        return [python, "-I", package, "update", "--scheduled"]
    if kind == "appimage" and os.path.isfile(package):
        return [package, "cli", "update", "--scheduled"]
    if kind == "macos-app":
        bundle = app_bundle(info["tree"])
        if bundle is not None and (bundle / "Contents" / "MacOS" / "sandbox-ai").is_file():
            return [str(bundle / "Contents" / "MacOS" / "sandbox-ai"), "update", "--scheduled"]
    return [sys.executable, "-I", str(HERE / "main.py"), "update", "--scheduled"]
