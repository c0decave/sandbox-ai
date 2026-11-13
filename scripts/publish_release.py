#!/usr/bin/env python3
"""Create (or update) a release on Gitea/Forgejo or GitHub and upload the dist/ files.

Usage: scripts/publish_release.py --source URL [--tag vX.Y.Z] [--token-env NAME] [--notes TEXT]
                                 [--sign KEY | --allow-unsigned] FILE...

  --source          repository URL, e.g. https://git.example.org/OWNER/sandbox-ai or https://github.com/OWNER/REPO
  --tag             default: v<version of sandbox/agent_sandbox.py>; the tag must already be pushed
  --token-env       host variable with an access token that may write releases (default: SANDBOX_AI_TOKEN)
  --sign KEY        sign the SHA256SUMS among the files first (SHA256SUMS.sig, uploaded along)
  --allow-unsigned  publish an unsigned SHA256SUMS although sandbox/release-signers lists keys

Before anything is uploaded, every file SHA256SUMS lists must match it, and when sandbox/release-signers lists
keys, SHA256SUMS.sig must be a good signature by one of them: installations would refuse the release otherwise.
The videos about the app's pages (sandbox-ai-videos.tar, make_dist.py --videos DIR) are optional; when they are among
the files, SHA256SUMS must list them (the app installs them only then).
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


def fail(message):
    sys.exit(f"publish_release: {message}")


def release_keys():
    spec = importlib.util.spec_from_file_location("sandbox_release_keys", ROOT / "sandbox" / "release_keys.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_files(files, key, allow_unsigned):
    """The files to upload, with SHA256SUMS.sig made (--sign) or checked: what install.sh will check, before the
    release changes."""
    rk = release_keys()
    try:
        keys = rk.load()
    except ValueError as exc:
        fail(str(exc))
    names = {path.name: path for path in files}
    sums = names.get("SHA256SUMS")
    if VIDEOS in names and sums is None:
        fail(f"{VIDEOS} without SHA256SUMS: the app would refuse it (make_dist.py --videos DIR)")
    if sums is None:
        if key:
            fail("--sign: no SHA256SUMS among the files")
        return files
    listed = {parts[1].lstrip("*"): parts[0] for parts in (line.split() for line in sums.read_text().splitlines())
              if len(parts) == 2}
    if VIDEOS in names and VIDEOS not in listed:
        fail(f"SHA256SUMS does not list {VIDEOS}: the app would refuse it (make_dist.py --sums-only --videos DIR)")
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


class Client:
    def __init__(self, token):
        self.token = token

    def request(self, method, url, body=None, content_type="application/json"):
        data = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"token {self.token}", "Accept": "application/json",
            "User-Agent": "sandbox-ai-release"})
        if data is not None:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
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
    parser.add_argument("--sign", metavar="KEY", type=Path, help="Sign SHA256SUMS with this SSH key first")
    parser.add_argument("--allow-unsigned", action="store_true",
                        help="Publish without a good signature although sandbox/release-signers lists keys")
    parser.add_argument("files", nargs="+", type=Path)
    a = parser.parse_args(argv)
    token = os.environ.get(a.token_env)
    if not token or not re.fullmatch(r"[A-Za-z0-9_.~+/=-]{8,512}", token):
        fail(f"set {a.token_env} to an access token that may write releases")
    tag = a.tag or f"v{version()}"
    for path in a.files:
        if not path.is_file():
            fail(f"not a file: {path}")
    files = check_files(a.files, a.sign, a.allow_unsigned)
    kind, api, uploads = endpoints(a.source)
    client = Client(token)
    release = client.request("GET", f"{api}/releases/tags/{urllib.parse.quote(tag)}")
    if release is None:
        release = client.request("POST", f"{api}/releases", {
            "tag_name": tag, "name": f"sandbox-ai {tag.lstrip('v')}", "body": a.notes,
            "draft": False, "prerelease": False})
        if release is None:
            fail(f"cannot create release {tag}: push the tag first (git push origin {tag})")
        print(f"publish_release: created release {tag}", file=sys.stderr)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
