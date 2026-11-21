#!/usr/bin/env python3
"""Check a release before it is announced: is every file there, listed in SHA256SUMS, signed, and does it work?

  scripts/release_check.py X.Y.Z                      the release vX.Y.Z on GitHub (github.com/c0decave/sandbox-ai)
  scripts/release_check.py X.Y.Z --source URL         ... of another repository (GitHub, Gitea, Forgejo)
  scripts/release_check.py X.Y.Z --dir dist           a folder with the release's files (before they are uploaded)
  scripts/release_check.py --dir dist --ci            what release.yml built: no videos, no signature yet
  ... --json                                          the same as a document for programs
  ... --no-try                                        only: there, listed, the SHA-256, the signature (nothing is
                                                      started or opened)
  ... --work DIR                                      where a release is downloaded to and tried out (default: a
                                                      temporary folder, removed afterwards; about 1 GB for a release)

A draft is found with a token that may see it (SANDBOX_AI_TOKEN, as for scripts/publish_release.py --draft): check
the draft, then publish it. The files come the way an update gets them (sandbox/package_update.py: the releases API,
the download links with their redirects), so what is checked is what an installation will be served.

For every file of the release (sandbox/release_assets.py) one line: is it there, does SHA256SUMS list it, does its
SHA-256 match, and a quick try where there is one:
  SHA256SUMS.sig   a good signature of SHA256SUMS by a key of this checkout's sandbox/release-signers
  the bundle       one top folder, the version, install.sh inside, and it trusts the key that signed the release
                   (installations take their keys for the next update from it)
  install.sh       bash -n; it carries the signing key for first installs
  sandbox-ai.pyz   started: --version says the version (in a cache of its own, not yours)
  the AppImage     of this machine's architecture: --appimage-extract of its manifest, which names the version
  the disk images  the form of a disk image (opened only on macOS: its app's version)
  wheel, sdist     the metadata: sandbox-ai-app, the version, the commands sandbox-ai and sandbox-ai-app
  the videos       only the series' files

Exit status 0: ready. 1: something is missing or wrong (each named). Standard library only (bash, ssh-keygen).
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = "https://github.com/c0decave/sandbox-ai"
VIDEO_NAME = re.compile(r"sandbox-ai-app-(\d{2})-([a-z]+)-([a-z]{2})\.(mp4|vtt)")  # make_dist.py's VIDEO_NAME


def load(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_release_check", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def own_version():
    text = (ROOT / "sandbox" / "agent_sandbox.py").read_text()
    return re.search(r'^__version__ = "([^"]+)"$', text, re.M)[1]


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def run(argv, seconds=600, **kwargs):
    return subprocess.run([str(word) for word in argv], capture_output=True, text=True, timeout=seconds,
                          stdin=subprocess.DEVNULL, **kwargs)


# -- the quick tries: (ok, text) --------------------------------------------------------------------------------------

def tree_file(tar, top, name):
    try:
        member = tar.getmember(f"{top}/{name}")
    except KeyError:
        return None
    return tar.extractfile(member).read() if member.isfile() else None


def try_bundle(path, version, signer, keys):
    with tarfile.open(path, "r:gz") as tar:
        tops = {member.name.split("/", 1)[0] for member in tar.getmembers()}
        if tops != {f"sandbox-ai-{version}"}:
            return False, f"its top folder is {', '.join(sorted(tops)[:3])}, not sandbox-ai-{version}"
        top = tops.pop()
        source = tree_file(tar, top, "sandbox/agent_sandbox.py") or b""
        found = re.search(rb'^__version__ = "([^"]+)"$', source, re.M)
        if not found or found[1].decode() != version:
            return False, f"says it is sandbox-ai {found[1].decode() if found else '?'}, not {version}"
        for name in ("install.sh", "sandbox-ai", "sandbox/main.py", "sandbox/updates.py", "sandbox/release_assets.py"):
            if tree_file(tar, top, name) is None:
                return False, f"lacks {name}"
        signers = tree_file(tar, top, "sandbox/release-signers") or b""
    try:
        listed = [keys.fingerprint(key)
                  for _, key in keys.parse(signers.decode("utf-8", "replace"), "its release-signers")]
    except ValueError as exc:
        return False, str(exc)
    if signer and signer not in listed:
        return False, (f"its sandbox/release-signers does not list the key that signed this release ({signer}): the "
                       "installations it makes would refuse the next one")
    return True, f"sandbox-ai {version}, " + (f"trusts the signing key and {len(listed) - 1} more" if signer else
                                              f"lists {len(listed)} release keys")


def try_installer(path, version, signer, keys):
    done = run(["bash", "-n", path], 60)
    if done.returncode != 0:
        return False, f"bash -n: {done.stderr.strip()[-200:]}"
    text = path.read_text(errors="replace")
    embedded = re.search(r"^EMBEDDED_SIGNERS='([^']*)'$", text, re.M)
    try:
        listed = [keys.fingerprint(key) for _, key in keys.parse(embedded[1] if embedded else "", "install.sh")]
    except ValueError as exc:
        return False, str(exc)
    if signer and signer not in listed:
        return False, (f"it does not carry the key that signed this release ({signer}): a first install could not "
                       "check it (make_dist.py writes the keys of sandbox/release-signers into it)")
    if f'DEFAULT_SOURCE="{DEFAULT_SOURCE}"' not in text:
        return False, f"its DEFAULT_SOURCE is not {DEFAULT_SOURCE}"
    return True, "bash -n, " + ("carries the signing key" if signer else f"carries {len(listed)} release keys")


def try_pyz(path, version, work):
    cache = Path(work) / "pyz-cache"
    cache.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("SANDBOX_AI_", "PYTHON"))}
    done = run([sys.executable, "-I", path, "--version"], env={**env, "XDG_CACHE_HOME": str(cache)})
    said = (done.stdout or "").strip()
    subprocess.run(["chmod", "-R", "u+w", str(cache)], capture_output=True, check=False)
    shutil.rmtree(cache, ignore_errors=True)
    if done.returncode != 0:
        return False, f"does not start: {(done.stderr or said).strip()[-200:]}"
    if said != f"sandbox-ai {version}":
        return False, f"says it is {said}, not {version}"
    return True, f"--version says sandbox-ai {version}"


def try_appimage(path, version, work, arch):
    assets = load("release_assets")
    if assets.arch_of("appimage") != arch or sys.platform != "linux":
        with open(path, "rb") as stream:
            head = stream.read(4)
        return (True, f"not run here (built for Linux on {arch})") if head in (b"\x7fELF", b"#!/b") else \
            (False, "is not a program (no ELF file)")
    folder = Path(tempfile.mkdtemp(prefix="appimage-", dir=work))
    try:
        program = folder / path.name
        shutil.copy2(path, program)
        program.chmod(0o755)
        done = run([program, "--appimage-extract", "usr/lib/sandbox-ai/manifest.json"], cwd=folder)
        manifest = folder / "squashfs-root/usr/lib/sandbox-ai/manifest.json"
        if done.returncode != 0 or not manifest.is_file():
            return False, f"--appimage-extract failed: {(done.stderr or done.stdout).strip()[-200:]}"
        data = json.loads(manifest.read_text())
        if data.get("kind") != "appimage" or data.get("version") != version:
            return False, f"says it is sandbox-ai {data.get('version')}, not {version}"
        return True, f"its manifest says appimage {version}"
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return False, f"could not be tried: {exc}"
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def try_dmg(path, version, work, arch):
    if sys.platform != "darwin" or not shutil.which("hdiutil"):
        with open(path, "rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - 512))
            trailer = stream.read(4)
            stream.seek(0)
            head = stream.read(2)
        if trailer == b"koly" or head == b"\x1f\x8b":  # (a disk image's trailer; the tests' stand-in is a tar.gz)
            return True, f"a disk image; not opened here (needs macOS, {arch})"
        return False, "is not a disk image (no koly trailer)"
    mount = Path(tempfile.mkdtemp(prefix="dmg-", dir=work))
    try:
        done = run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mount, path])
        if done.returncode != 0:
            return False, f"hdiutil attach failed: {done.stderr.strip()[-200:]}"
        try:
            source = mount / "sandbox-ai.app/Contents/Resources/sandbox-ai/sandbox/agent_sandbox.py"
            found = re.search(r'^__version__ = "([^"]+)"$', source.read_text(errors="replace"), re.M)
            if not found or found[1] != version:
                return False, f"its app says it is sandbox-ai {found[1] if found else '?'}, not {version}"
            if not (mount / "Applications").is_symlink():
                return False, "has no link to /Applications"
            return True, f"sandbox-ai.app {version} and the link to Applications"
        finally:
            run(["hdiutil", "detach", "-quiet", mount])
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not be opened: {exc}"
    finally:
        shutil.rmtree(mount, ignore_errors=True)


def metadata(text):
    name, found = re.search(r"^Name: (.+)$", text, re.M), re.search(r"^Version: (.+)$", text, re.M)
    return (name[1].strip() if name else None), (found[1].strip() if found else None)


def try_wheel(path, version):
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        info = f"sandbox_ai_app-{version}.dist-info"
        if f"{info}/METADATA" not in names:
            return False, f"has no {info}/METADATA"
        name, found = metadata(archive.read(f"{info}/METADATA").decode("utf-8", "replace"))
        entries = archive.read(f"{info}/entry_points.txt").decode() if f"{info}/entry_points.txt" in names else ""
        lacking = [wanted for wanted in ("sandbox_ai/main.py", "sandbox_ai/updates.py", "sandbox_ai/package_update.py",
                                         "sandbox_ai/release_assets.py", "sandbox_ai/release-signers",
                                         "sandbox_ai_app/__main__.py") if wanted not in names]
    if (name, found) != ("sandbox-ai-app", version):
        return False, f"says it is {name} {found}, not sandbox-ai-app {version}"
    if lacking:
        return False, f"lacks {', '.join(lacking)}"
    commands = [command for command in ("sandbox-ai", "sandbox-ai-app") if re.search(rf"^{command} = ", entries, re.M)]
    if len(commands) != 2:
        return False, "does not install the commands sandbox-ai and sandbox-ai-app"
    return True, f"sandbox-ai-app {version}, the commands sandbox-ai and sandbox-ai-app"


def try_sdist(path, version):
    top = f"sandbox_ai_app-{version}"
    with tarfile.open(path, "r:gz") as tar:
        tops = {member.name.split("/", 1)[0] for member in tar.getmembers()}
        if tops != {top}:
            return False, f"its top folder is {', '.join(sorted(tops)[:3])}, not {top}"
        info = tree_file(tar, top, "PKG-INFO")
        name, found = metadata((info or b"").decode("utf-8", "replace"))
        if (name, found) != ("sandbox-ai-app", version):
            return False, f"says it is {name} {found}, not sandbox-ai-app {version}"
        if tree_file(tar, top, "pyproject.toml") is None or tree_file(tar, top, "sandbox/main.py") is None:
            return False, "lacks pyproject.toml or sandbox/main.py"
    return True, f"sandbox-ai-app {version}, with its sources"


def try_videos(path):
    with tarfile.open(path, "r:") as tar:
        members = tar.getmembers()
    strange = [member.name for member in members if not member.isfile() or not VIDEO_NAME.fullmatch(member.name)]
    if strange:
        return False, f"holds what is no video of the series: {', '.join(strange[:3])}"
    videos = sum(member.name.endswith(".mp4") for member in members)
    if not videos:
        return False, "holds no video"
    subtitles = len(members) - videos
    return True, f"{videos} video{'s' if videos != 1 else ''}, {subtitles} subtitle file{'s' if subtitles != 1 else ''}"


def quick_try(entry, path, version, signer, keys, work):
    kind = entry["kind"]
    try:
        if kind == "bundle":
            return try_bundle(path, version, signer, keys)
        if kind == "installer":
            return try_installer(path, version, signer, keys)
        if kind == "pyz":
            return try_pyz(path, version, work)
        if kind == "appimage":
            return try_appimage(path, version, work, entry["arch"])
        if kind == "dmg":
            return try_dmg(path, version, work, entry["arch"])
        if kind == "wheel":
            return try_wheel(path, version)
        if kind == "sdist":
            return try_sdist(path, version)
        if kind == "videos":
            return try_videos(path)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile, subprocess.SubprocessError, KeyError) as exc:
        return False, f"could not be read: {str(exc)[:200]}"
    return True, ""


# -- the release ------------------------------------------------------------------------------------------------------

def folder_files(folder):
    return {path.name: path for path in sorted(Path(folder).iterdir()) if path.is_file() and not path.is_symlink()}


def find_on_source(source, version, updater):
    """(release document, problem) of vVERSION at SOURCE: published, or - with a token - its draft."""
    api, url, github = updater.api_base(source)
    host = "api.github.com" if github else url.netloc
    try:
        with updater.open_url(f"{api}/releases/tags/v{version}", host) as response:
            return json.loads(response.read(updater.API_LIMIT)), None
    except updater.NotFound:
        pass
    drafts = []
    if os.environ.get("SANDBOX_AI_TOKEN"):
        try:
            with updater.open_url(f"{api}/releases?per_page=100&limit=50", host) as response:
                listing = json.loads(response.read(updater.API_LIMIT))
        except updater.NotFound:
            listing = []
        drafts = [item for item in listing if isinstance(item, dict) and item.get("draft")
                  and item.get("tag_name") == f"v{version}"] if isinstance(listing, list) else []
    if drafts:
        return drafts[0], None
    return None, (f"no release v{version} at {source}" + (
        "" if os.environ.get("SANDBOX_AI_TOKEN") else " (a draft is only shown to a token that may see it: "
                                                      "SANDBOX_AI_TOKEN)"))


def download_release(document, source, work, updater, say):
    """The release's files in WORK, fetched as an update fetches them: {name: path}."""
    api, url, github = updater.api_base(source)
    token, files = os.environ.get("SANDBOX_AI_TOKEN"), {}
    for asset in document.get("assets") or []:
        name = asset.get("name") if isinstance(asset, dict) else None
        if not isinstance(name, str) or "/" in name or name.startswith("."):
            continue
        api_url = asset.get("url")
        # (a draft's files are served to a token through the API only, like a private repository's)
        if github and token and isinstance(api_url, str) and api_url.startswith(f"{api}/releases/assets/"):
            link = (api_url, "api.github.com")
        else:
            link = (asset.get("browser_download_url"), url.netloc)
        say(f"  downloading {name}")
        updater.download(*link, str(Path(work) / name))
        files[name] = Path(work) / name
    return files


def examine(files, version, ci, work, release=None, tries=True):
    """The report of the files FILES ({name: path}) as release VERSION (TRIES: with the quick try of each)."""
    assets, keys = load("release_assets"), load("release_keys")
    problems, rows = [], []
    sums_path, signature_path = files.get(assets.SUMS), files.get(assets.SIGNATURE)
    listed = assets.checksums(sums_path.read_text(errors="replace")) if sums_path else {}
    signature = {"state": "none", "signer": None}
    if ci:
        signature["state"] = "not expected (CI holds no release key)"
    elif sums_path is None:
        problems.append("SHA256SUMS is missing")
    elif signature_path is None:
        problems.append("SHA256SUMS.sig is missing: installations refuse an unsigned release")
    else:
        try:
            trusted = keys.load()
            verdict, detail = keys.verify(sums_path.read_bytes(), signature_path.read_bytes(), trusted) if trusted \
                else ("bad", "this checkout's sandbox/release-signers lists no key")
        except (ValueError, RuntimeError) as exc:
            verdict, detail = "bad", str(exc)
        signature = {"state": verdict, "signer": detail if verdict == "good" else None}
        if verdict != "good":
            problems.append(f"SHA256SUMS.sig: {detail}")
    signer = signature["signer"]
    signed_version = assets.bundle_version(listed)
    if sums_path is not None and signed_version != version:
        problems.append(f"SHA256SUMS lists " + (f"the bundle of {signed_version}" if signed_version else "no bundle")
                        + f", not the one of {version}: updates take a release's version from it")
    wanted = assets.artifacts(version)
    if ci:
        wanted = [entry for entry in wanted if entry["by"] == assets.CI]
    for entry in wanted:
        name, path = entry["name"], files.get(entry["name"])
        row = {"name": name, "present": path is not None, "listed": name in listed, "sha256": None, "test": None}
        if path is None:
            problems.append(f"{name} is missing")
        else:
            if name not in listed:
                problems.append(f"{name} is not listed in SHA256SUMS")
            else:
                row["sha256"] = "ok" if sha256(path) == listed[name] else "differs"
                if row["sha256"] != "ok":
                    problems.append(f"{name} does not match SHA256SUMS")
            if tries:
                ok, text = quick_try(entry, path, version, signer, keys, work)
                row["test"] = f"ok: {text}" if ok else f"FAILED: {text}"
                if not ok:
                    problems.append(f"{name} {text}")
        rows.append(row)
    rows.append({"name": assets.SUMS, "present": sums_path is not None, "listed": None, "sha256": None,
                 "test": f"lists {len(listed)} files" if sums_path else None})
    if not ci:
        rows.append({"name": assets.SIGNATURE, "present": signature_path is not None, "listed": None, "sha256": None,
                     "test": (f"ok: a good signature by {signer}" if signer else None)})
    known = set(assets.required(version))
    for name in sorted(set(files) | set(listed)):
        if name in known:
            continue
        if assets.BUNDLE.fullmatch(name) or name.startswith("sandbox_ai_app-") or name.startswith("sandbox_ai-"):
            problems.append(f"{name} is not a file of release {version} (another version's?)")
        elif name in files and name not in listed:
            problems.append(f"{name} is not listed in SHA256SUMS (nobody could check it)")
        elif name not in files:
            problems.append(f"SHA256SUMS lists {name}, which is not there")
    return {"version": version, "ready": not problems, "ci": ci, "tried": tries, "problems": problems,
            "signature": signature, "files": rows, "release": release}


def table(report, out):
    width = max(len(row["name"]) for row in report["files"])
    out(f"{'file'.ljust(width)}  there  listed  sha256  try")
    for row in report["files"]:
        cells = ["yes" if row["present"] else "NO",
                 "-" if row["listed"] is None else "yes" if row["listed"] else "NO",
                 row["sha256"] or "-", row["test"] or "-"]
        out(f"{row['name'].ljust(width)}  {cells[0].ljust(5)}  {cells[1].ljust(6)}  {cells[2].ljust(6)}  {cells[3]}")
    out("")
    release = report.get("release")
    if release:
        state = "a draft: not published yet (scripts/publish_release.py --publish)" if release["draft"] else \
            "published" + (", and the newest release (what installations are served)" if release["latest"] else
                           ", but NOT what releases/latest names")
        out(f"release {release['tag']} at {release['source']}: {state}")
    version = report["version"]
    if report["problems"]:
        for problem in report["problems"]:
            out(f"  - {problem}")
        out(f"sandbox-ai {version}: NOT READY ({len(report['problems'])} to fix)")
    elif report["ci"]:
        out(f"sandbox-ai {version}: every file CI builds is there, listed in SHA256SUMS and works; the videos and the "
            "signature come where the release is signed (RELEASING.md)")
    else:
        out(f"sandbox-ai {version}: READY - every file is there, listed in SHA256SUMS, signed by "
            f"{report['signature']['signer']}" + (", and works" if report["tried"] else " (--no-try: nothing was "
                                                                                       "started)"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check a release: every file there, listed, signed and working")
    parser.add_argument("version", nargs="?", help="X.Y.Z (default: this checkout's version)")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--dir", type=Path, help="A folder with the release's files instead of a published release")
    where.add_argument("--source", help=f"The repository with the release (default: {DEFAULT_SOURCE})")
    parser.add_argument("--ci", action="store_true", help="Only what release.yml builds: no videos, no signature")
    parser.add_argument("--work", type=Path, help="Where to download and try (default: a temporary folder)")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    parser.add_argument("--no-try", action="store_true",
                        help="Only whether each file is there, listed and matches; nothing is started or opened")
    a = parser.parse_args(argv)
    version = (a.version or own_version()).lstrip("v")
    if not re.fullmatch(load("release_assets").VERSION, version):
        sys.exit("release_check: the version is X.Y.Z")
    say = (lambda text: print(text, file=sys.stderr))
    own_work = a.work is None
    work = Path(tempfile.mkdtemp(prefix="sandbox-ai-release-check.")) if own_work else a.work
    work.mkdir(parents=True, exist_ok=True)
    # Absolute: the tries start copies inside WORK with WORK as their folder (a relative --work named a path that is
    # not there from inside it: the AppImage "could not be tried")
    work = work.resolve()
    if a.dir is not None:
        a.dir = a.dir.resolve()
    try:
        release = None
        if a.dir is not None:
            if not a.dir.is_dir():
                sys.exit(f"release_check: no folder {a.dir}")
            files = folder_files(a.dir)
        else:
            updater = load("package_update")
            source = (a.source or DEFAULT_SOURCE).rstrip("/")
            try:
                document, problem = find_on_source(source, version, updater)
                if document is None:
                    print(f"release_check: {problem}")
                    return 1
                say(f"release_check: {document.get('tag_name')} at {source}: {len(document.get('assets') or [])} files")
                files = download_release(document, source, work, updater, say)
                latest = False
                if not document.get("draft"):
                    try:
                        latest = updater.release(source)["tag"] == document.get("tag_name")
                    except updater.UpdateError:
                        latest = False
            except updater.UpdateError as exc:
                print(f"release_check: {exc}")
                return 1
            release = {"tag": document.get("tag_name"), "draft": bool(document.get("draft")), "latest": latest,
                       "source": source}
        report = examine(files, version, a.ci, work, release, tries=not a.no_try)
    finally:
        if own_work:
            shutil.rmtree(work, ignore_errors=True)
    if a.json:
        print(json.dumps(report, indent=2))
    else:
        table(report, print)
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
