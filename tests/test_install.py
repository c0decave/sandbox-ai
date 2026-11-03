"""install.sh, scripts/make_dist.py and scripts/publish_release.py against a fake Gitea server."""
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.search(r'^__version__ = "([^"]+)"$', (ROOT / "sandbox/agent_sandbox.py").read_text(), re.M)[1]
COMMANDS = ["sandbox-ai", "claude-sandbox", "codex-sandbox", "opencode-sandbox", "sandbox-session", "sandbox-mcp",
            "sandbox-wizard", "sandbox-build-image", "sandbox-ai-update"]
TOKEN = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist")
    subprocess.run([sys.executable, str(ROOT / "scripts/make_dist.py"), "--allow-dirty", "--skip-python",
                    "--out", str(out)], check=True, capture_output=True, env={**os.environ, "SOURCE_DATE_EPOCH": "0"})
    return out


class FakeGitea(http.server.ThreadingHTTPServer):
    """Releases API of one repository o/r, plus downloads and uploads; records Authorization headers."""
    def __init__(self, files):
        super().__init__(("127.0.0.1", 0), Handler)
        self.files, self.auth, self.releases, self.uploads = dict(files), [], {}, []

    def release(self, tag):
        base = f"http://127.0.0.1:{self.server_address[1]}"
        return {"id": 1, "tag_name": tag, "draft": False, "prerelease": False,
                "assets": [{"id": i, "name": name, "browser_download_url": f"{base}/o/r/releases/download/{tag}/{name}"}
                           for i, name in enumerate(sorted(self.files), 1)]}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body=b"", kind="application/json"):
        body = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.auth.append((self.path, self.headers.get("Authorization")))
        tag = f"v{VERSION}"
        if self.path in ("/api/v1/repos/o/r/releases/latest", f"/api/v1/repos/o/r/releases/tags/{tag}"):
            return self.reply(200, self.server.release(tag))
        if self.path.startswith("/api/v1/repos/o/r/releases/tags/"):
            return self.reply(404, {"message": "not found"})
        match = re.fullmatch(r"/o/r/releases/download/[^/]+/(.+)", self.path)
        if match and match[1] in self.server.files:
            return self.reply(200, self.server.files[match[1]], "application/octet-stream")
        self.reply(404, {"message": "not found"})

    def do_POST(self):
        self.server.auth.append((self.path, self.headers.get("Authorization")))
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/api/v1/repos/o/r/releases":
            return self.reply(201, {"id": 1, "tag_name": json.loads(body)["tag_name"], "assets": []})
        if self.path.startswith("/api/v1/repos/o/r/releases/1/assets"):
            self.server.uploads.append((self.path, self.headers.get("Content-Type"), body))
            return self.reply(201, {"id": len(self.server.uploads)})
        self.reply(404)

    def do_DELETE(self):
        self.server.auth.append((self.path, self.headers.get("Authorization")))
        self.server.uploads.append((self.path, "DELETE", b""))
        self.reply(204)


@pytest.fixture
def gitea(dist):
    server = FakeGitea({p.name: p.read_bytes() for p in dist.iterdir()})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def environment(tmp_path, **extra):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("XDG_", "SANDBOX_AI_"))}
    return {**env, "HOME": str(home), "PATH": "/usr/bin:/bin", **extra}


def install(tmp_path, *args, **extra):
    return subprocess.run(["bash", str(ROOT / "install.sh"), "--user", *args], capture_output=True, text=True,
                          env=environment(tmp_path, **extra), timeout=120)


def installed(tmp_path):
    return tmp_path / "home/.local/share/sandbox-ai"


def test_install_from_checkout(scenario, tmp_path):
    if scenario == "edge":
        (tmp_path / "home").mkdir()
        (tmp_path / "home/.bin").mkdir()  # an existing ~/.bin gets the links too
    result = install(tmp_path, "--from", str(ROOT))
    assert result.returncode == 0, result.stderr
    data, bins = installed(tmp_path), [tmp_path / "home/.local/bin"]
    if scenario == "edge":
        bins.append(tmp_path / "home/.bin")
    for directory in bins:
        for command in COMMANDS:
            link = directory / command
            assert link.is_symlink() and str(os.readlink(link)).startswith(str(data)), link
    assert (data / "current").resolve() == data / "versions" / VERSION
    version = subprocess.run([str(bins[0] / "claude-sandbox"), "--version"], capture_output=True, text=True)
    assert version.stdout.strip() == f"sandbox-ai {VERSION}"
    # No local tool state or VCS data travels along.
    assert not (data / "current/.git").exists() and not (data / "current/.claude").exists()
    # The installed copy updates itself through its installer.
    check = subprocess.run([str(bins[0] / "sandbox-ai"), "update", "--user", "--check", "--from", str(ROOT)],
                           capture_output=True, text=True, env=environment(tmp_path))
    assert check.returncode == 0 and f"installed: {VERSION}" in check.stdout, check.stderr
    if scenario == "sad":
        # Uninstall removes links and data, nothing else.
        (tmp_path / "home/.local/bin/other-tool").write_text("keep")
        result = install(tmp_path, "--uninstall")
        assert result.returncode == 0, result.stderr
        assert not data.exists() and not any((tmp_path / "home/.local/bin" / c).exists() for c in COMMANDS)
        assert (tmp_path / "home/.local/bin/other-tool").read_text() == "keep"


def test_install_latest_release_with_checksum(scenario, tmp_path, gitea):
    source = f"http://127.0.0.1:{gitea.server_address[1]}/o/r"
    bundle = f"sandbox-ai-bundle-{VERSION}.tar.gz"
    if scenario == "sad":
        # A tampered bundle is refused before anything is installed.
        gitea.files[bundle] = gitea.files[bundle][:-10] + b"tampered!!"
        result = install(tmp_path, "--source", source)
        assert result.returncode != 0 and "checksum mismatch" in result.stderr
        assert not (installed(tmp_path) / "current").exists()
        return
    token = {"SANDBOX_AI_TOKEN": TOKEN} if scenario == "edge" else {}
    result = install(tmp_path, "--source", source, **token)
    assert result.returncode == 0, result.stderr
    assert "SHA-256 verified" in result.stderr and (installed(tmp_path) / "versions" / VERSION).is_dir()
    assert TOKEN not in result.stdout + result.stderr
    sent = {auth for _, auth in gitea.auth}
    assert sent == ({f"token {TOKEN}"} if scenario == "edge" else {None})
    # Running it again changes nothing: already the newest.
    again = install(tmp_path, "--source", source, **token)
    assert again.returncode == 0 and "up to date" in again.stderr
    check = install(tmp_path, "--source", source, "--check")
    assert f"installed: {VERSION}" in check.stdout and f"available: {VERSION}" in check.stdout


def test_install_protects_foreign_files_and_refuses_bad_input(scenario, tmp_path, gitea):
    bin_dir = tmp_path / "home/.local/bin"
    bin_dir.mkdir(parents=True)
    if scenario == "happy":
        (bin_dir / "claude-sandbox").write_text("#!/bin/sh\necho mine\n")
        result = install(tmp_path, "--from", str(ROOT))
        assert result.returncode == 0 and "does not belong to sandbox-ai" in result.stderr
        assert (bin_dir / "claude-sandbox").read_text() == "#!/bin/sh\necho mine\n"
        forced = install(tmp_path, "--from", str(ROOT), "--force")
        assert forced.returncode == 0 and (bin_dir / "claude-sandbox").is_symlink()
    elif scenario == "sad":
        # The token never goes over plain http to anything but localhost.
        result = install(tmp_path, "--source", "http://git.example.invalid/o/r", SANDBOX_AI_TOKEN=TOKEN)
        assert result.returncode != 0 and "refusing to send SANDBOX_AI_TOKEN over plain http" in result.stderr
        assert TOKEN not in result.stderr
        assert "--version expects" in install(tmp_path, "--version", "1.0; rm -rf /").stderr
    else:
        # A bundle with an escaping entry is refused.
        evil = tmp_path / "evil.tar.gz"
        with tarfile.open(evil, "w:gz") as tar:
            payload = tmp_path / "x"
            payload.write_text("x")
            tar.add(payload, arcname="sandbox-ai-9/../../escape")
        result = install(tmp_path, "--from", str(evil))
        assert result.returncode != 0 and "unsafe entry" in result.stderr
        assert not (tmp_path / "escape").exists()


def test_make_dist_is_reproducible(scenario, tmp_path, dist):
    other = tmp_path / "again"
    env = {**os.environ, "SOURCE_DATE_EPOCH": "0"}
    subprocess.run([sys.executable, str(ROOT / "scripts/make_dist.py"), "--allow-dirty", "--skip-python",
                    "--out", str(other)], check=True, capture_output=True, env=env)
    name = f"sandbox-ai-bundle-{VERSION}.tar.gz"
    assert (other / name).read_bytes() == (dist / name).read_bytes()
    sums = dict(reversed(line.split("  ")) for line in (dist / "SHA256SUMS").read_text().splitlines())
    assert sums[name] == hashlib.sha256((dist / name).read_bytes()).hexdigest()
    with tarfile.open(dist / name) as tar:
        names = tar.getnames()
        if scenario == "happy":
            assert f"sandbox-ai-{VERSION}/install.sh" in names and f"sandbox-ai-{VERSION}/sandbox/agent_sandbox.py" in names
        elif scenario == "sad":
            assert not any("/.claude/" in n or "/.git/" in n or "__pycache__" in n for n in names)
        else:
            assert all(m.uid == 0 and m.gid == 0 and m.mtime == 0 for m in tar.getmembers())


def test_publish_release(scenario, tmp_path, gitea, dist):
    source = f"http://127.0.0.1:{gitea.server_address[1]}/o/r"
    files = [str(dist / "SHA256SUMS"), str(dist / "install.sh")]
    env = {**os.environ, "RELEASE_TOKEN": TOKEN}
    args = [sys.executable, str(ROOT / "scripts/publish_release.py"), "--source", source, "--token-env", "RELEASE_TOKEN"]
    if scenario == "sad":
        result = subprocess.run([*args, *files], capture_output=True, text=True, env={**env, "RELEASE_TOKEN": ""})
        assert result.returncode != 0 and "RELEASE_TOKEN" in result.stderr
        result = subprocess.run([*args[:3], "http://git.example.invalid/o/r", "--token-env", "RELEASE_TOKEN", *files],
                                capture_output=True, text=True, env=env)
        assert result.returncode != 0 and "plain http" in result.stderr
        return
    tag = ["--tag", "v9.9.9"] if scenario == "edge" else []
    result = subprocess.run([*args, *tag, *files], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert all(auth == f"token {TOKEN}" for _, auth in gitea.auth)
    uploads = [u for u in gitea.uploads if u[1] != "DELETE"]
    assert [re.search(r"name=([^&]+)", u[0])[1] for u in uploads] == ["SHA256SUMS", "install.sh"]
    assert all(u[1].startswith("multipart/form-data") and b'name="attachment"' in u[2] for u in uploads)
    if scenario == "happy":
        # The release v<version> already had these files: they are replaced, not duplicated.
        assert [u for u in gitea.uploads if u[1] == "DELETE"]
        assert "created release" not in result.stderr
    else:
        assert "created release v9.9.9" in result.stderr
