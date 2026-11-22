"""A fake GitHub for the update tests: the releases API of one repository, the release files' downloads with
GitHub's redirects, and the uploads, served over TLS on this machine - reached by the real, unchanged updater code
(install.sh, sandbox/updates.py, scripts/publish_release.py and release_check.py, the app) through the two things
every HTTPS client honours: https_proxy (a CONNECT proxy here, which takes every GitHub name to the fake) and
SSL_CERT_FILE (the fake's own certificate). So `--source https://github.com/OWNER/REPO` - the default - is what the
tests run, with no test switch in the code under test.

  api.github.com                        /repos/O/R/releases/latest, /releases/tags/TAG, /releases, /releases/ID,
                                        /releases/assets/ID (the file with Accept: application/octet-stream)
  github.com                            /O/R/releases/download/TAG/NAME and /releases/latest/download/NAME: a
                                        redirect to the file's host, as GitHub answers
  release-assets.githubusercontent.com  the files (no token ever arrives there: `leaked` lists what did)
  uploads.github.com                    /repos/O/R/releases/ID/assets?name=NAME

Needs the openssl command for the certificate (`available()`); standard library otherwise.
"""
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
import urllib.parse

HOSTS = ("github.com", "api.github.com", "uploads.github.com", "release-assets.githubusercontent.com",
         "objects.githubusercontent.com")
FILES_HOST = "release-assets.githubusercontent.com"


def available():
    return shutil.which("openssl") is not None


def make_certificate(folder):
    """(certificate, key) for every name of HOSTS, self-signed: SSL_CERT_FILE names the certificate."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    cert, key = folder / "fake-github.pem", folder / "fake-github.key"
    if cert.is_file() and key.is_file():  # (one certificate for a test session)
        return cert, key
    names = ",".join(f"DNS:{host}" for host in HOSTS)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "3", "-subj", "/CN=fake github (sandbox-ai tests)", "-addext",
                    f"subjectAltName={names}", "-addext", "basicConstraints=critical,CA:TRUE"],
                   check=True, capture_output=True, env={**os.environ, "RANDFILE": str(folder / ".rnd")})
    return cert, key


class Tunnel(socketserver.BaseRequestHandler):
    """CONNECT HOST:443 of the proxy: a tunnel to the fake when HOST is one of GitHub's names, 403 otherwise."""

    def handle(self):
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 65536:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            head += chunk
        words = head.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(words) < 2 or words[0] != "CONNECT":
            self.server.fake.refused.append(" ".join(words[:2]))
            self.request.sendall(b"HTTP/1.1 400 only CONNECT\r\nContent-Length: 0\r\n\r\n")
            return
        host = words[1].rsplit(":", 1)[0]
        self.server.fake.connects.append(words[1])
        if host not in HOSTS:
            self.server.fake.refused.append(words[1])
            self.request.sendall(b"HTTP/1.1 403 not GitHub\r\nContent-Length: 0\r\n\r\n")
            return
        try:
            upstream = socket.create_connection(("127.0.0.1", self.server.fake.tls_port), timeout=30)
        except OSError:
            self.request.sendall(b"HTTP/1.1 502 no fake\r\nContent-Length: 0\r\n\r\n")
            return
        self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        rest = head.split(b"\r\n\r\n", 1)[1]
        if rest:
            upstream.sendall(rest)
        pair = [self.request, upstream]
        try:
            while True:
                ready, _, _ = select.select(pair, [], [], 120)
                if not ready:
                    return
                for sock in ready:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is self.request else self.request).sendall(data)
        except OSError:
            return
        finally:
            upstream.close()


class Proxy(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Api(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        try:
            self.request.do_handshake()
        except (OSError, ssl.SSLError):
            pass

    # -- answers --

    def reply(self, code, body=b"", kind="application/json", headers=()):
        body = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect(self, url):
        self.reply(302, b"", headers=(("Location", url),))

    def send_file(self, content):
        if isinstance(content, (bytes, bytearray)):
            return self.reply(200, bytes(content), "application/octet-stream")
        size = os.path.getsize(content)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with open(content, "rb") as stream:
            shutil.copyfileobj(stream, self.wfile, 1 << 20)

    def seen(self):
        fake = self.server.fake
        host = (self.headers.get("Host") or "").split(":")[0]
        token = self.headers.get("Authorization")
        fake.requests.append((self.command, host, self.path, token))
        if token and host == FILES_HOST:
            fake.leaked.append((host, self.path))
        return fake, host, urllib.parse.urlsplit(self.path)

    def limited(self, fake, host):
        if fake.rate_limited and host == "api.github.com":
            self.reply(403, {"message": "API rate limit exceeded"},
                       headers=(("X-RateLimit-Remaining", "0"), ("X-RateLimit-Reset", "4102444800")))
            return True
        return False

    # -- the methods --

    def do_GET(self):
        fake, host, url = self.seen()
        if self.limited(fake, host):
            return None
        repo = f"/repos/{fake.owner}/{fake.repo}"
        authorised = self.headers.get("Authorization") is not None
        if host == "api.github.com":
            if url.path == f"{repo}/releases/latest":
                release = fake.newest()
                return self.reply(200, fake.document(release)) if release else self.reply(404, {"message": "Not Found"})
            match = re.fullmatch(re.escape(repo) + r"/releases/tags/(.+)", url.path)
            if match:  # (a draft has no tag yet: GitHub does not find it by one)
                release = fake.releases.get(urllib.parse.unquote(match[1]))
                if release and not release["draft"]:
                    return self.reply(200, fake.document(release))
                return self.reply(404, {"message": "Not Found"})
            if url.path == f"{repo}/releases":
                return self.reply(200, [fake.document(release) for release in reversed(list(fake.releases.values()))
                                        if authorised or not release["draft"]])
            match = re.fullmatch(re.escape(repo) + r"/releases/([0-9]+)", url.path)
            if match:
                release = fake.by_id(int(match[1]))
                if release and (authorised or not release["draft"]):
                    return self.reply(200, fake.document(release))
                return self.reply(404, {"message": "Not Found"})
            match = re.fullmatch(re.escape(repo) + r"/releases/assets/([0-9]+)", url.path)
            if match and int(match[1]) in fake.assets:
                release, name = fake.assets[int(match[1])]
                if release["draft"] and not authorised:
                    return self.reply(404, {"message": "Not Found"})
                if "application/octet-stream" in (self.headers.get("Accept") or ""):
                    return self.redirect(f"https://{FILES_HOST}/asset/{match[1]}?sig=fake")
                return self.reply(200, fake.asset_document(release, name))
            return self.reply(404, {"message": "Not Found"})
        if host in ("github.com", "www.github.com"):
            base = f"/{fake.owner}/{fake.repo}/releases"
            match = re.fullmatch(re.escape(base) + r"/latest/download/(.+)", url.path)
            if match and fake.newest():
                return self.redirect(f"https://github.com{base}/download/{fake.newest()['tag']}/{match[1]}")
            match = re.fullmatch(re.escape(base) + r"/download/([^/]+)/(.+)", url.path)
            release = fake.releases.get(urllib.parse.unquote(match[1])) if match else None
            if release and not release["draft"] and match[2] in release["files"]:
                return self.redirect(f"https://{FILES_HOST}/asset/{release['ids'][match[2]]}?sig=fake")
            return self.reply(404, b"Not Found", "text/plain")
        if host in (FILES_HOST, "objects.githubusercontent.com"):
            match = re.fullmatch(r"/asset/([0-9]+)", url.path)
            if match and int(match[1]) in fake.assets:
                release, name = fake.assets[int(match[1])]
                fake.downloads.append(name)
                return self.send_file(release["files"][name])
            return self.reply(404, b"Not Found", "text/plain")
        return self.reply(404, b"no such host here", "text/plain")

    do_HEAD = do_GET

    def body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def do_POST(self):
        fake, host, url = self.seen()
        body = self.body()
        repo = f"/repos/{fake.owner}/{fake.repo}"
        if not self.headers.get("Authorization"):
            return self.reply(401, {"message": "Requires authentication"})
        if host == "api.github.com" and url.path == f"{repo}/releases":
            asked = json.loads(body)
            if asked["tag_name"] not in fake.pushed_tags:
                return self.reply(422, {"message": "Validation Failed: tag does not exist"})
            release = fake.create(asked["tag_name"], draft=bool(asked.get("draft")),
                                  prerelease=bool(asked.get("prerelease")), body=asked.get("body", ""))
            return self.reply(201, fake.document(release))
        match = re.fullmatch(re.escape(repo) + r"/releases/([0-9]+)/assets", url.path)
        if host == "uploads.github.com" and match and fake.by_id(int(match[1])):
            release = fake.by_id(int(match[1]))
            name = urllib.parse.parse_qs(url.query).get("name", [""])[0]
            if name in release["files"]:
                return self.reply(422, {"message": "Validation Failed", "errors": [{"code": "already_exists"}]})
            fake.add_file(release, name, body)
            fake.uploads.append((release["tag"], name))
            return self.reply(201, fake.asset_document(release, name))
        return self.reply(404, {"message": "Not Found"})

    def do_PATCH(self):
        fake, host, url = self.seen()
        body = self.body()
        match = re.fullmatch(rf"/repos/{re.escape(fake.owner)}/{re.escape(fake.repo)}/releases/([0-9]+)", url.path)
        release = fake.by_id(int(match[1])) if match and host == "api.github.com" else None
        if release is None or not self.headers.get("Authorization"):
            return self.reply(404, {"message": "Not Found"})
        asked = json.loads(body)
        for key in ("draft", "prerelease", "body"):
            if key in asked:
                release[key] = asked[key]
        fake.patches.append((release["tag"], asked))
        return self.reply(200, fake.document(release))

    def do_DELETE(self):
        fake, host, url = self.seen()
        match = re.fullmatch(rf"/repos/{re.escape(fake.owner)}/{re.escape(fake.repo)}/releases/assets/([0-9]+)",
                             url.path)
        if host == "api.github.com" and match and int(match[1]) in fake.assets and self.headers.get("Authorization"):
            release, name = fake.assets.pop(int(match[1]))
            release["files"].pop(name, None)
            release["ids"].pop(name, None)
            fake.deleted.append((release["tag"], name))
            return self.reply(204)
        return self.reply(404, {"message": "Not Found"})


class TlsServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


class FakeGitHub:
    """GitHub's side of the repository OWNER/REPO. publish(VERSION, FILES) makes a release (FILES: {name: bytes or
    a path}); the newest published one that is no draft and no pre-release is "latest". env() gives the variables a
    process needs to reach it as https://github.com/OWNER/REPO."""

    def __init__(self, folder, owner="c0decave", repo="sandbox-ai"):
        self.owner, self.repo = owner, repo
        self.source = f"https://github.com/{owner}/{repo}"
        self.releases, self.assets, self.pushed_tags = {}, {}, set()
        self.requests, self.connects, self.refused, self.leaked = [], [], [], []
        self.downloads, self.uploads, self.deleted, self.patches = [], [], [], []
        self.rate_limited, self.next_id = False, 100
        self.certificate, key = make_certificate(folder)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.certificate), str(key))
        self.tls = TlsServer(("127.0.0.1", 0), Api)
        self.tls.fake = self
        self.tls.socket = context.wrap_socket(self.tls.socket, server_side=True, do_handshake_on_connect=False)
        self.tls_port = self.tls.server_address[1]
        self.proxy = Proxy(("127.0.0.1", 0), Tunnel)
        self.proxy.fake = self
        self.proxy_url = f"http://127.0.0.1:{self.proxy.server_address[1]}"
        for server in (self.tls, self.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def close(self):
        for server in (self.proxy, self.tls):
            server.shutdown()
            server.server_close()

    # -- what a process needs --

    def env(self, base=None):
        """BASE (default: this process's environment) with the proxy and the certificate of the fake, and nothing
        that would lead past it."""
        env = dict(os.environ if base is None else base)
        for name in ("no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY", "SANDBOX_AI_SOURCE"):
            env.pop(name, None)
        env.update(https_proxy=self.proxy_url, HTTPS_PROXY=self.proxy_url, http_proxy=self.proxy_url,
                   HTTP_PROXY=self.proxy_url, SSL_CERT_FILE=str(self.certificate),
                   CURL_CA_BUNDLE=str(self.certificate), REQUESTS_CA_BUNDLE=str(self.certificate))
        return env

    # -- the releases --

    def create(self, tag, draft=False, prerelease=False, body=""):
        self.next_id += 1
        release = {"id": self.next_id, "tag": tag, "draft": draft, "prerelease": prerelease, "body": body,
                   "files": {}, "ids": {}}
        self.releases[tag] = release
        return release

    def add_file(self, release, name, content):
        self.next_id += 1
        release["files"][name] = content
        release["ids"][name] = self.next_id
        self.assets[self.next_id] = (release, name)

    def publish(self, version, files, draft=False, prerelease=False):
        """The release vVERSION with FILES ({name: bytes or a path}, or a folder: every file in it)."""
        if not isinstance(files, dict):
            files = {path.name: path for path in sorted(Path(files).iterdir()) if path.is_file()}
        tag = f"v{version}"
        self.pushed_tags.add(tag)
        release = self.create(tag, draft, prerelease)
        for name, content in files.items():
            self.add_file(release, name, content)
        return release

    def publish_recorded(self, document, files):
        """A release as GitHub's releases API answered it (DOCUMENT, a recorded copy, served as it is for
        releases/latest and releases/tags/TAG) with FILES ({name: bytes or a path}) behind its download links."""
        release = self.publish(str(document["tag_name"]).lstrip("v"), files)
        release["recorded"] = document
        return release

    def newest(self):
        published = [release for release in self.releases.values() if not release["draft"] and
                     not release["prerelease"]]
        return published[-1] if published else None

    def by_id(self, number):
        return next((release for release in self.releases.values() if release["id"] == number), None)

    def asset_document(self, release, name):
        content = release["files"][name]
        size = len(content) if isinstance(content, (bytes, bytearray)) else os.path.getsize(content)
        digest = hashlib.sha256(content if isinstance(content, (bytes, bytearray)) else
                                Path(content).read_bytes()).hexdigest() if size < (8 << 20) else None
        return {"id": release["ids"][name], "name": name, "size": size, "state": "uploaded",
                "digest": f"sha256:{digest}" if digest else None,
                "url": f"https://api.github.com/repos/{self.owner}/{self.repo}/releases/assets/{release['ids'][name]}",
                "browser_download_url": f"https://github.com/{self.owner}/{self.repo}/releases/download/"
                                        f"{release['tag']}/{name}"}

    def document(self, release):
        if release.get("recorded") is not None:
            return release["recorded"]
        return {"id": release["id"], "tag_name": release["tag"], "name": f"sandbox-ai {release['tag'].lstrip('v')}",
                "draft": release["draft"], "prerelease": release["prerelease"], "body": release["body"],
                "html_url": f"https://github.com/{self.owner}/{self.repo}/releases/tag/{release['tag']}",
                "assets": [self.asset_document(release, name) for name in sorted(release["files"])]}
