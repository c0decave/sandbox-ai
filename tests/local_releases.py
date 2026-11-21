"""Releases of sandbox-ai served on 127.0.0.1 for the macOS live check (tests/test_live_macos.py): a Gitea-style
releases API (/api/v1/repos/live/sandbox-ai/releases/latest, /releases/tags/vX.Y.Z, /releases) and the files, over
plain http - which sandbox-ai's updater allows to this machine only (package_update.open_url). `SANDBOX_AI_SOURCE`
names it (source); nothing else is reached and nothing leaves the machine.

publish(version, files): the release vVERSION with FILES {name: a path, bytes, or None - listed, but its download
answers 404 (a release's bundle the macOS app never downloads, when only its disk image is at hand)}. `downloads`
lists the names of the files fetched. Standard library only.
"""
import http.server
import json
from pathlib import Path
import threading
import urllib.parse

OWNER, REPO = "live", "sandbox-ai"


class LocalReleases:
    def __init__(self):
        self.releases, self.downloads, self.requests = {}, [], []
        releases = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                releases.requests.append(self.path)
                status, body, kind = releases.answer(urllib.parse.urlsplit(self.path).path)
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.source = f"{self.base}/{OWNER}/{REPO}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def publish(self, version, files):
        self.releases[version] = dict(files)

    def document(self, version):
        return {"tag_name": f"v{version}", "name": f"sandbox-ai {version}", "draft": False, "prerelease": False,
                "assets": [{"name": name, "browser_download_url": f"{self.base}/files/v{version}/{name}"}
                           for name in sorted(self.releases[version])]}

    def answer(self, path):
        api = f"/api/v1/repos/{OWNER}/{REPO}/releases"
        newest = list(self.releases)[-1:] if self.releases else []
        if path == f"{api}/latest" and newest:
            return 200, json.dumps(self.document(newest[0])).encode(), "application/json"
        if path == api:
            return 200, json.dumps([self.document(v) for v in reversed(list(self.releases))]).encode(), \
                "application/json"
        if path.startswith(f"{api}/tags/v") and path[len(f"{api}/tags/v"):] in self.releases:
            return 200, json.dumps(self.document(path[len(f"{api}/tags/v"):])).encode(), "application/json"
        parts = path.split("/")
        if len(parts) == 4 and parts[1] == "files" and parts[2][1:] in self.releases:
            content = self.releases[parts[2][1:]].get(parts[3])
            if content is not None:
                self.downloads.append(parts[3])
                data = content if isinstance(content, bytes) else Path(content).read_bytes()
                return 200, data, "application/octet-stream"
        return 404, b'{"message": "not found"}', "application/json"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
