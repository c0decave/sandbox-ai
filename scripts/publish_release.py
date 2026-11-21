#!/usr/bin/env python3
"""Create (or update) a release on Gitea/Forgejo or GitHub and upload the dist/ files.

Usage: scripts/publish_release.py --source URL [--tag vX.Y.Z] [--token-env NAME] [--notes TEXT | --notes-file PATH]
                                 [--sign KEY | --allow-unsigned] [--draft] [--partial] FILE...
       scripts/publish_release.py --source URL [--tag vX.Y.Z] [--token-env NAME] --publish

  --source          repository URL, e.g. https://git.example.org/OWNER/sandbox-ai or https://github.com/OWNER/REPO
  --tag             default: v<version of sandbox/agent_sandbox.py>; the tag must already be pushed
  --token-env       host variable with an access token that may write releases (default: SANDBOX_AI_TOKEN)
  --sign KEY        sign the SHA256SUMS among the files first (SHA256SUMS.sig, uploaded along)
  --allow-unsigned  publish an unsigned SHA256SUMS although sandbox/release-signers lists keys
  --draft           leave the release a draft (nobody is served it): check it with scripts/release_check.py, then
                    --publish
  --publish         no files: publish the draft of the tag, once it holds every file of the release
  --partial         a release that lacks files (your own Gitea or Forgejo, without the packages only CI builds);
                    never for GitHub

A release is complete or it is not published: the files must be every file of the release (sandbox/release_assets.py:
both AppImages, both disk images, sandbox-ai.pyz, the wheel and the sdist, the bundle, install.sh, the videos,
SHA256SUMS and, when sandbox/release-signers lists keys, SHA256SUMS.sig) - each lacking one is named and nothing is
uploaded (--partial, never for GitHub: said, and uploaded without them).
Before anything is uploaded, SHA256SUMS must list every file and every file must match it, and when
sandbox/release-signers lists keys, SHA256SUMS.sig must be a good signature by one of them: installations would
refuse the release otherwise (files without SHA256SUMS only with --allow-unsigned and --partial).
A new release is made as a draft and published after its last file is up, so `releases/latest` - what install.sh and
every `sandbox-ai update` ask - never names a release whose files are still on their way.
Files with the same name in an existing release are replaced. The token is sent only to the
repository's API host (and GitHub's upload host), never printed, never over plain http except to localhost.
"""
import argparse
import hashlib
import importlib.util
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
VIDEOS = "sandbox-ai-videos.tar"  # make_dist.py VIDEOS


GITHUB_BODY_LIMIT = 125_000  # characters of a release's body; GitHub refuses more


def fail(message):
    sys.exit(f"publish_release: {message}")


def release_keys():
    spec = importlib.util.spec_from_file_location("sandbox_release_keys", ROOT / "sandbox" / "release_keys.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def release_assets():
    """sandbox/release_assets.py: the files of a release, in one place."""
    spec = importlib.util.spec_from_file_location("sandbox_release_assets", ROOT / "sandbox" / "release_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_complete(names, version, signed, partial=False, kind=None, what="the files"):
    """Stop unless NAMES are every file of release VERSION (PARTIAL: the lacking ones are only said - never for
    GitHub), and none is another release's."""
    assets = release_assets()
    if partial and kind == "github":
        fail("--partial is not for GitHub: a release there always carries every file (RELEASING.md, the checklist)")
    for name in names:
        other = assets.BUNDLE.fullmatch(name) or re.fullmatch(
            r"sandbox_ai_app-(" + assets.VERSION + r")(?:-py3-none-any\.whl|\.tar\.gz)", name)
        if other and other[1] != version:
            fail(f"{name} is not a file of release {version} (a folder with files of two versions? build dist/ anew)")
    lacking = assets.missing(names, version, signed=signed)
    if not lacking:
        return
    if not partial:
        fail(f"release {version} lacks {', '.join(lacking)} among {what}: nothing uploaded. A release carries every "
             "file (RELEASING.md, the checklist; scripts/release_check.py names what a folder holds); --partial "
             "uploads without them to your own Gitea or Forgejo")
    print(f"publish_release: --partial: release {version} goes up WITHOUT {', '.join(lacking)}", file=sys.stderr)


def check_files(files, key, allow_unsigned, version=None, partial=None, kind=None):
    """The files to upload, with SHA256SUMS.sig made (--sign) or checked: what install.sh will check, before the
    release changes. VERSION: they must be every file of that release (check_complete; PARTIAL: or only some)."""
    rk = release_keys()
    try:
        keys = rk.load()
    except ValueError as exc:
        fail(str(exc))
    names = {path.name: path for path in files}
    if version is not None:
        check_complete([*names, *(["SHA256SUMS.sig"] if key else [])], version, bool(keys) and not allow_unsigned,
                       bool(partial), kind)
    sums = names.get("SHA256SUMS")
    if VIDEOS in names and sums is None:
        fail(f"{VIDEOS} without SHA256SUMS: the app would refuse it (make_dist.py --videos DIR)")
    if sums is None:
        if key:
            fail("--sign: no SHA256SUMS among the files")
        if keys and not allow_unsigned:
            fail("no SHA256SUMS among the files, but sandbox/release-signers lists keys: nothing would check what is "
                 "uploaded. Upload them with SHA256SUMS (make_dist.py --sums-only --add FILE ...), or pass "
                 "--allow-unsigned")
        return files
    listed = {parts[1].lstrip("*"): parts[0] for parts in (line.split() for line in sums.read_text().splitlines())
              if len(parts) == 2}
    if VIDEOS in names and VIDEOS not in listed:
        fail(f"SHA256SUMS does not list {VIDEOS}: the app would refuse it (make_dist.py --sums-only --videos DIR)")
    unlisted = sorted(name for name in names if name not in listed and name not in ("SHA256SUMS", "SHA256SUMS.sig"))
    if unlisted:
        fail(f"SHA256SUMS does not list {', '.join(unlisted)}: nobody could check it against the signed checksums "
             "(make_dist.py --sums-only --add FILE ... writes them anew)")
    for name, path in names.items():
        if name in listed and hashlib.sha256(path.read_bytes()).hexdigest() != listed[name]:
            fail(f"{path} does not match SHA256SUMS (rebuild dist/ with scripts/make_dist.py)")
    sig = names.get("SHA256SUMS.sig")
    if key:
        try:
            signature = rk.sign(sums.read_bytes(), key)
        except RuntimeError as exc:
            fail(str(exc))
        sig = sums.with_name("SHA256SUMS.sig")
        sig.write_bytes(signature)
        files = [path for path in files if path.name != "SHA256SUMS.sig"] + [sig]
    if sig is None:
        if keys and not allow_unsigned:
            fail("SHA256SUMS is not signed, but sandbox/release-signers lists keys: installations would refuse this "
                 "release. Pass --sign KEY (or SHA256SUMS.sig from make_dist.py --sign)")
        return files
    verdict, detail = rk.verify(sums.read_bytes(), sig.read_bytes(), keys)
    if verdict != "good" and not (allow_unsigned and not key):
        fail(f"SHA256SUMS.sig: {detail}" + ("" if keys else " (sandbox/release-signers lists no key)"))
    if verdict == "good":
        print(f"publish_release: SHA256SUMS signed by {detail}", file=sys.stderr)
    return files


def version():
    match = re.search(r'^__version__ = "([^"]+)"$', (ROOT / "sandbox/agent_sandbox.py").read_text(), re.M)
    return match[1]


def endpoints(source):
    """(kind, api base, uploads base) for a repository URL."""
    url = urllib.parse.urlsplit(re.sub(r"\.git$", "", source.rstrip("/")))
    parts = [p for p in url.path.split("/") if p]
    if url.scheme not in ("http", "https") or len(parts) < 2 or not url.hostname:
        fail(f"--source must be http(s)://HOST/OWNER/REPO: {source!r}")
    owner, repo, root = parts[-2], parts[-1], "/".join(parts[:-2])
    if url.hostname in ("github.com", "www.github.com"):
        return "github", f"https://api.github.com/repos/{owner}/{repo}", f"https://uploads.github.com/repos/{owner}/{repo}"
    if url.scheme == "http" and not (url.hostname == "localhost" or is_loopback(url.hostname)):
        fail("refusing to send the token over plain http to a remote host; use https")
    base = f"{url.scheme}://{url.netloc}" + (f"/{root}" if root else "")
    return "gitea", f"{base}/api/v1/repos/{owner}/{repo}", None


def is_loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class SameOrigin(urllib.request.HTTPRedirectHandler):
    """Redirects within the origin of the request only (scheme, host and port): urllib carries every header of a
    request over to where a redirect points, the token's Authorization included."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            fail(f"{req.get_method()} {req.full_url}: HTTP {code} redirect to {new.scheme}://{new.netloc}, another "
                 "host: not followed (the token goes only to the repository's API host)")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(SameOrigin)

    def request(self, method, url, body=None, content_type="application/json"):
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"token {self.token}", "Accept": "application/json",
            "User-Agent": "sandbox-ai-release"})
        if data is not None:
            request.add_header("Content-Type", content_type)
        try:
            with self.opener.open(request, timeout=300) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            detail = exc.read()[:500].decode(errors="replace")
            fail(f"{method} {url}: HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, OSError) as exc:
            fail(f"{method} {url}: {getattr(exc, 'reason', exc)}")
        return json.loads(payload) if payload.strip() else {}


def multipart(field, path):
    boundary = uuid.uuid4().hex
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{path.name}\"\r\n"
            f"Content-Type: {kind}\r\n\r\n").encode()
    return head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Publish dist/ files as a release")
    parser.add_argument("--source", required=True)
    parser.add_argument("--tag")
    parser.add_argument("--token-env", default="SANDBOX_AI_TOKEN")
    parser.add_argument("--notes", default="")
    parser.add_argument("--notes-file", type=Path,
                        help="The release page from a file (release_notes.py's page is too long for an argument)")
    parser.add_argument("--sign", metavar="KEY", type=Path, help="Sign SHA256SUMS with this SSH key first")
    parser.add_argument("--allow-unsigned", action="store_true",
                        help="Publish without a good signature although sandbox/release-signers lists keys")
    parser.add_argument("--draft", action="store_true",
                        help="Leave the release a draft: check it (scripts/release_check.py), then --publish")
    parser.add_argument("--publish", action="store_true",
                        help="No files: publish the tag's draft, once it holds every file of the release")
    parser.add_argument("--partial", action="store_true",
                        help="A release that lacks files (your own Gitea or Forgejo; never GitHub)")
    parser.add_argument("files", nargs="*", type=Path)
    a = parser.parse_args(argv)
    token = os.environ.get(a.token_env)
    if not token or not re.fullmatch(r"[A-Za-z0-9_.~+/=-]{8,512}", token):
        fail(f"set {a.token_env} to an access token that may write releases")
    tag = a.tag or f"v{version()}"
    if bool(a.files) == a.publish:
        fail("give the release's files, or --publish alone (it publishes the tag's draft)")
    if a.publish and (a.draft or a.sign):
        fail("--publish takes no files and no other change: it publishes the draft as it is")
    for path in a.files:
        if not path.is_file():
            fail(f"not a file: {path}")
    kind, api, uploads = endpoints(a.source)
    if a.notes_file is not None:
        if a.notes:
            fail("give --notes or --notes-file, not both")
        try:
            a.notes = a.notes_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            fail(f"cannot read --notes-file {a.notes_file}: {exc}")
    if kind == "github" and len(a.notes) > GITHUB_BODY_LIMIT:
        fail(f"the release page has {len(a.notes):,} characters, GitHub takes at most {GITHUB_BODY_LIMIT:,}: "
             f"shorten docs/releases/v{(a.tag or 'v' + version())[1:]}.md (scripts/release_notes.py)")
    release_version = tag[1:] if tag.startswith("v") else tag
    try:
        signed = bool(release_keys().load()) and not a.allow_unsigned
    except ValueError as exc:
        fail(str(exc))
    client = Client(token)
    if a.publish:
        return publish_draft(client, api, tag, release_version, signed, a.partial, kind)
    files = check_files(a.files, a.sign, a.allow_unsigned, release_version, a.partial, kind)
    release = find_release(client, api, tag)
    if release is None:
        # A draft first: releases/latest - what every installation asks - names it only once every file is up.
        release = client.request("POST", f"{api}/releases", {
            "tag_name": tag, "name": f"sandbox-ai {release_version}", "body": a.notes,
            "draft": True, "prerelease": False})
        if release is None:
            fail(f"cannot create release {tag}: push the tag first (git push origin {tag})")
        print(f"publish_release: created release {tag} as a draft", file=sys.stderr)
    existing = {asset["name"]: asset["id"] for asset in release.get("assets", [])}

    def delete(name):
        client.request("DELETE", f"{api}/releases/assets/{existing[name]}" if kind == "github"
                       else f"{api}/releases/{release['id']}/assets/{existing[name]}")

    names = {path.name for path in files}
    if "SHA256SUMS" in names and "SHA256SUMS.sig" not in names and "SHA256SUMS.sig" in existing:
        delete("SHA256SUMS.sig")  # it signed the SHA256SUMS that is replaced now
        print("publish_release: removed the old SHA256SUMS.sig", file=sys.stderr)
    for path in files:
        if path.name in existing:
            delete(path.name)
        if kind == "github":
            client.request("POST", f"{uploads}/releases/{release['id']}/assets?name={urllib.parse.quote(path.name)}",
                           path.read_bytes(), "application/octet-stream")
        else:
            body, content_type = multipart("attachment", path)
            client.request("POST", f"{api}/releases/{release['id']}/assets?name={urllib.parse.quote(path.name)}",
                           body, content_type)
        print(f"publish_release: uploaded {path.name}", file=sys.stderr)
    if a.draft:
        if not release.get("draft"):
            print(f"publish_release: release {tag} was published before: it stays published", file=sys.stderr)
            return 0
        print(f"publish_release: release {tag} is a draft with {len(files)} files. Check it: scripts/release_check.py "
              f"{release_version} --source {a.source} (with the token), then: scripts/publish_release.py --source "
              f"{a.source} --publish", file=sys.stderr)
        return 0
    if release.get("draft"):
        return publish_draft(client, api, tag, release_version, signed, a.partial, kind, release["id"])
    return 0


def find_release(client, api, tag):
    """The release of TAG: the published one, else its draft (GitHub and Gitea find a draft by no tag: the list of
    releases, which shows drafts to a token that may write them)."""
    release = client.request("GET", f"{api}/releases/tags/{urllib.parse.quote(tag)}")
    if release is not None:
        return release
    listing = client.request("GET", f"{api}/releases?per_page=100&limit=50")
    drafts = [item for item in listing
              if isinstance(item, dict) and item.get("draft") and item.get("tag_name") == tag] \
        if isinstance(listing, list) else []
    if len(drafts) > 1:
        fail(f"{len(drafts)} drafts for {tag}: remove all but one on the releases page, then run this again")
    return drafts[0] if drafts else None


def publish_draft(client, api, tag, version, signed, partial, kind, number=None):
    """The draft of TAG published - after the release itself, asked anew, lists every file of the release."""
    release = client.request("GET", f"{api}/releases/{number}") if number is not None else \
        find_release(client, api, tag)
    if release is None:
        fail(f"no release for {tag} at this source (publish its files first)")
    if not release.get("draft"):
        print(f"publish_release: release {tag} is published already", file=sys.stderr)
        return 0
    assets = [asset for asset in release.get("assets", []) if isinstance(asset, dict)]
    check_complete([asset.get("name") for asset in assets], version, signed, partial, kind, "the draft's files")
    unfinished = [asset.get("name") for asset in assets if asset.get("state") not in (None, "uploaded")
                  or asset.get("size") == 0]
    if unfinished:
        fail(f"the draft of {tag} holds unfinished uploads ({', '.join(map(str, unfinished))}): upload them again")
    done = client.request("PATCH", f"{api}/releases/{release['id']}", {"draft": False})
    if done is None or done.get("draft"):
        fail(f"the draft of {tag} could not be published (it is still a draft: publish it on the releases page)")
    print(f"publish_release: published release {tag} ({len(assets)} files)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
